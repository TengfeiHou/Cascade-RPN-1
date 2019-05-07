import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import normal_init

from mmdet.core import delta2bbox
from mmdet.ops import nms, DeformConv
from .cascade_anchor_head import CascadeAnchorHead
from ..registry import HEADS
from ..utils import bias_init_with_prob


@HEADS.register_module
class CascadeRPNHead(CascadeAnchorHead):

    def __init__(self, in_channels, feat_adapt=False, dilation=1,
                 bridged_feature=False, **kwargs):
        super(CascadeRPNHead, self).__init__(2, in_channels, **kwargs)
        self.feat_adapt = feat_adapt
        self.dilation = dilation
        self.bridged_feature = bridged_feature
        self._init_layers()

    def _init_layers(self):
        if self.feat_adapt:
            assert self.dilation == 1
            self.adapt_conv = DeformConv(
                self.feat_channels, self.feat_channels, 3, padding=1)
        else:
            self.rpn_conv = nn.Conv2d(self.in_channels,
                                      self.feat_channels,
                                      3,
                                      padding=self.dilation,
                                      dilation=self.dilation)
        if self.with_cls:
            self.rpn_cls = nn.Conv2d(self.feat_channels,
                                     self.num_anchors * self.cls_out_channels,
                                     1)
        self.rpn_reg = nn.Conv2d(self.feat_channels, self.num_anchors * 4, 1)

    def init_weights(self):
        normal_init(self.rpn_reg, std=0.01)
        if self.with_cls:
            if self.use_focal_loss:
                cls_bias = bias_init_with_prob(0.01)
                normal_init(self.rpn_cls, std=0.01, bias=cls_bias)
            else:
                normal_init(self.rpn_cls, std=0.01)
        if self.feat_adapt:
            normal_init(self.adapt_conv, std=0.01)
        else:
            normal_init(self.rpn_conv, std=0.01)

    def forward_single(self, x, offset):
        if self.feat_adapt:
            assert offset is not None
            N, _, H, W = x.shape
            assert H * W == offset.shape[1]
            # reshape [N, NA, 18] to (N, 18, H, W)
            offset = offset.permute(0, 2, 1).reshape(N, -1, H, W)
            x = self.adapt_conv(x, offset)
        else:
            x = self.rpn_conv(x)
        x = F.relu(x, inplace=True)
        out = ()
        if self.bridged_feature:
            out = out + (x,)
        if self.with_cls:
            rpn_cls_score = self.rpn_cls(x)
            out = out + (rpn_cls_score,)
        rpn_bbox_pred = self.rpn_reg(x)
        out = out + (rpn_bbox_pred,)
        return out

    def loss(self,
             anchor_list,
             valid_flag_list,
             cls_scores,
             bbox_preds,
             gt_bboxes,
             img_metas,
             cfg,
             loss_weight=1,
             gt_bboxes_ignore=None):
        losses = super(CascadeRPNHead, self).loss(
            anchor_list,
            valid_flag_list,
            cls_scores,
            bbox_preds,
            gt_bboxes,
            None,
            img_metas,
            cfg,
            loss_weight=loss_weight,
            gt_bboxes_ignore=gt_bboxes_ignore)
        if self.with_cls:
            return dict(
                loss_rpn_cls=losses['loss_cls'],
                loss_rpn_reg=losses['loss_reg'])
        return dict(loss_rpn_reg=losses['loss_reg'])

    def get_bboxes_single(self,
                          cls_scores,
                          bbox_preds,
                          mlvl_anchors,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False):
        mlvl_proposals = []
        for idx in range(len(cls_scores)):
            rpn_cls_score = cls_scores[idx]
            rpn_bbox_pred = bbox_preds[idx]
            assert rpn_cls_score.size()[-2:] == rpn_bbox_pred.size()[-2:]
            anchors = mlvl_anchors[idx]
            rpn_cls_score = rpn_cls_score.permute(1, 2, 0)
            if self.use_sigmoid_cls:
                rpn_cls_score = rpn_cls_score.reshape(-1)
                scores = rpn_cls_score.sigmoid()
            else:
                rpn_cls_score = rpn_cls_score.reshape(-1, 2)
                scores = rpn_cls_score.softmax(dim=1)[:, 1]
            rpn_bbox_pred = rpn_bbox_pred.permute(1, 2, 0).reshape(-1, 4)
            if cfg.nms_pre > 0 and scores.shape[0] > cfg.nms_pre:
                _, topk_inds = scores.topk(cfg.nms_pre)
                rpn_bbox_pred = rpn_bbox_pred[topk_inds, :]
                anchors = anchors[topk_inds, :]
                scores = scores[topk_inds]
            proposals = delta2bbox(anchors, rpn_bbox_pred, self.target_means,
                                   self.target_stds, img_shape)
            if cfg.min_bbox_size > 0:
                w = proposals[:, 2] - proposals[:, 0] + 1
                h = proposals[:, 3] - proposals[:, 1] + 1
                valid_inds = torch.nonzero((w >= cfg.min_bbox_size) &
                                           (h >= cfg.min_bbox_size)).squeeze()
                proposals = proposals[valid_inds, :]
                scores = scores[valid_inds]
            proposals = torch.cat([proposals, scores.unsqueeze(-1)], dim=-1)
            proposals, _ = nms(proposals, cfg.nms_thr)
            proposals = proposals[:cfg.nms_post, :]
            mlvl_proposals.append(proposals)
        proposals = torch.cat(mlvl_proposals, 0)
        if cfg.nms_across_levels:
            proposals, _ = nms(proposals, cfg.nms_thr)
            proposals = proposals[:cfg.max_num, :]
        else:
            scores = proposals[:, 4]
            num = min(cfg.max_num, proposals.shape[0])
            _, topk_inds = scores.topk(num)
            proposals = proposals[topk_inds, :]
        return proposals

# encoding: utf-8
"""
@author:  sherlock
@contact: sherlockliao01@gmail.com
"""

import logging
try:
    # Capirca uses Google's abseil-py library, which uses a Google-specific
    # wrapper for logging. That wrapper will write a warning to sys.stderr if
    # the Google command-line flags library has not been initialized.
    #
    # https://github.com/abseil/abseil-py/blob/pypi-v0.7.1/absl/logging/__init__.py#L819-L825
    #
    # This is not right behavior for Python code that is invoked outside of a
    # Google-authored main program. Use knowledge of abseil-py to disable that
    # warning; ignore and continue if something goes wrong.
    import absl.logging

    # https://github.com/abseil/abseil-py/issues/99
    logging.root.removeHandler(absl.logging._absl_handler)
    # https://github.com/abseil/abseil-py/issues/102
    absl.logging._warn_preinit_stderr = False
except Exception:
    pass

import torch
import torch.nn as nn

import torchvision

from ignite.engine import Engine, Events
from ignite.handlers import Timer #ModelCheckpoint,  #自己编写一下ModelCheckpoint，使其可以设置初始轮数
from solver.checkpoint import ModelCheckpoint

from ignite.metrics import RunningAverage
from ignite.metrics import Accuracy
from ignite.metrics import Precision

from engine.evaluator import do_inference

from torch.utils.tensorboard import SummaryWriter

from solver import WarmupMultiStepLR

import torch.nn.functional as F
import copy
import random

"""
try:
    from tensorboardX import SummaryWriter
except ImportError:
    raise RuntimeError(
        "No tensorboardX package is found. Please install with the command: \npip install tensorboardX")
"""


#CJY at 2019.9.24 既然这里面定义的与inference.py 中的一样能不能直接引用
#还没加

global ITER
ITER = 0

global epochs_traverse_optimizers
epochs_traverse_optimizers = 0

global epochs_per_optimizer
epochs_per_optimizer = 500

global op2loss
op2loss = {0:"D", 1:"G"}

global weight
weight = 1


global model2
model2 = None

def transfer_weights(model_from, model_to):
    wf = copy.deepcopy(model_from.state_dict())
    wt = model_to.state_dict()
    for k in wt.keys() :
        #if (not k in wf)):
        if ((not k in wf) | (k=='fc.weight') | (k=='fc.bias')):
            wf[k] = wt[k]
    model_to.load_state_dict(wf)


# 创建multilabel
def label2multilabel(label):
    num_classes = 6
    label_groups = [[0, 1, 2], [3, 4, 5]]

    #创建label到对应的multilabel tensor的字典
    label_dict = {}
    for lg in label_groups:
        t = torch.zeros(1, num_classes)
        for l in lg:
            t[0][l] = 1
        for l in lg:
            label_dict[l] = t

    #依据label创建multilabel
    for i in range(label.shape[0]):
        if i == 0:
            multilabel = label_dict[label[i].item()]
        else:
            multilabel = torch.cat([multilabel, label_dict[label[i].item()]], dim=0)

    return multilabel



def create_supervised_trainer(model, optimizers, metrics, loss_fn, device=None,):
    """
    Factory function for creating a trainer for supervised models

    Args:
        model (`torch.nn.Module`): the model to train
        optimizer (`torch.optim.Optimizer`): the optimizer to use
        loss_fn (torch.nn loss function): the loss function to use
        device (str, optional): device type specification (default: None).
            Applies to both model and batches.

    Returns:
        Engine: a trainer engine with supervised update function
    """
    if device:
        if torch.cuda.device_count() > 1:
            model = nn.DataParallel(model)
        model.to(device)
        #optimizer加载进来的是cpu类型，需要手动转成gpu。
        for state in optimizers[0].state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.cuda()

        if model.reloadState == True:
            model2 = copy.deepcopy(model)

    epochs_traverse_optimizers = len(optimizers) * epochs_per_optimizer

    def _update(engine, batch):
        model.train()

        # 分为D和G不同的优化器（暂时无用）
        schedulers_epochs_index = (engine.state.epoch - 1) // epochs_traverse_optimizers
        index = (engine.state.epoch - 1) % epochs_traverse_optimizers  # 注意engine.state.epoch从1开始
        phase_index = index // epochs_per_optimizer
        # 记录在state中，方便传递给handler
        engine.state.optimizer_index = phase_index#(phase_index + 1) % len(optimizers)  #加了一个偏置，先训练G
        engine.state.schedulers_epochs_index = schedulers_epochs_index
        engine.state.epochs_traverse_optimizers = epochs_traverse_optimizers
        engine.state.losstype = op2loss[engine.state.optimizer_index]

        # 获取数据
        grade_imgs, grade_labels, seg_imgs, seg_gt_masks, seg_labels = batch   #这个格式应该跟collate_fn的处理方式对应
        #seg_gt_masks = torch.gt(seg_gt_masks, 0).float()
        # 记录grade和seg的样本数量
        grade_num = grade_imgs.shape[0]
        seg_num = seg_gt_masks.shape[0]
        # 将grade和seg样本concat起来
        imgs = torch.cat([grade_imgs, seg_imgs], dim=0)
        labels = torch.cat([grade_labels, seg_labels], dim=0)
        # 置入cuda
        #one_hot_labels = torch.nn.functional.one_hot(grade_labels, scores.shape[1]).float()
        #grade_imgs = grade_imgs.to(device) if torch.cuda.device_count() >= 1 else grade_imgs
        grade_labels = grade_labels.to(device) if torch.cuda.device_count() >= 1 else grade_labels
        seg_labels = seg_labels.to(device) if torch.cuda.device_count() >= 1 else seg_labels
        seg_gt_masks = seg_gt_masks.to(device) if torch.cuda.device_count() >= 1 else seg_gt_masks
        imgs = imgs.to(device) if torch.cuda.device_count() >= 1 else imgs
        labels = labels.to(device) if torch.cuda.device_count() >= 1 else labels
        # 将label转为one - hot
        one_hot_labels = torch.nn.functional.one_hot(labels, model.num_classes).float()
        one_hot_labels = one_hot_labels.to(device) if torch.cuda.device_count() >= 1 else one_hot_labels

        # Master 0 运行模型  (内置运行 Branch 1 Segmentation)
        # 设定有多少样本需要进行支路的运算
        """
        model.transimitBatchDistribution((grade_num+seg_num-model.branch_img_num, model.branch_img_num))
        model.transmitClassifierWeight()   #如果是BOF 会回传分类器权重
        logits = model(imgs)               #为了减少显存，还是要区分grade和seg
        grade_logits = logits[0:grade_num]
        #"""

        # 提前进行样本扩增  CJY at 2020.4.5
        #"""
        soft_mask = seg_gt_masks
        soft_mask = model.lesionFusion(soft_mask, labels[labels.shape[0] - soft_mask.shape[0]:labels.shape[0]])
        max_kernel_size = random.randint(30, 160)
        soft_mask = torch.nn.functional.max_pool2d(soft_mask, kernel_size=max_kernel_size * 2 + 1, stride=1,
                                                   padding=max_kernel_size)

        rimgs = imgs[imgs.shape[0] - soft_mask.shape[0]:imgs.shape[0]].clone()
        rimg_mean = rimgs.mean(-1, keepdim=True).mean(-2, keepdim=True)
        pos_masked_img = soft_mask * rimgs  # + (1-soft_mask) * rimg_mean
        neg_masked_img = (1 - soft_mask) * rimgs  # + soft_mask * rimg_mean
        imgs[imgs.shape[0] - soft_mask.shape[0]:imgs.shape[0]] = pos_masked_img

        # 也考虑normal吧
        for i in range(grade_num):
            if labels[i] == 0:
                imgs[i:i+1] = imgs[i:i+1] * soft_mask

        model.transimitBatchDistribution(0)
        model.transmitClassifierWeight()   #如果是BOF 会回传分类器权重
        logits = model(imgs)               #为了减少显存，还是要区分grade和seg
        grade_logits = logits[0:grade_num]
        #"""


        # Branch 1 Segmentation
        if model.segState == True:
            seg_masks = torch.sigmoid(model.base.seg_attention)  # [model.base.seg_attention.shape[0]-seg_num: model.base.seg_attention.shape[0]]
            if model.segSupervisedType != "none":
                seg_gtmasks = seg_gt_masks
            else:
                seg_gtmasks = None
        else:
            seg_masks = None
            seg_gtmasks = None

        # Branch 2 Grad-CAM
        if model.gcamState == True:
            # 将label转为one - hot
            #one_hot_labels = torch.nn.functional.one_hot(labels, model.num_classes).float()
            #one_hot_labels = one_hot_labels.to(device) if torch.cuda.device_count() >= 1 else one_hot_labels

            # 求取model.inter_output对应的gradient
            # 回传one-hot向量, 可直接传入想要获取梯度的inputs列表，返回也是列表
            inter_gradients = torch.autograd.grad(outputs=logits, inputs=model.inter_output,
                                                     grad_outputs=one_hot_labels, retain_graph=True, create_graph=True)
            model.inter_gradient = list(inter_gradients)



            # 生成CAM
            gcam_list = []
            target_layer_num = len(model.target_layer)

            for i in range(target_layer_num):
                inter_output = model.inter_output[i][model.inter_output[i].shape[0]-model.batchDistribution[1]:model.inter_output[i].shape[0]]  # 此处分离节点，别人皆不分离  .detach()
                inter_gradient = model.inter_gradient[i][model.inter_gradient[i].shape[0]-model.batchDistribution[1]:model.inter_gradient[i].shape[0]]
                if model.target_layer[i] == "denseblock4" and model.hierarchyClassifier==0:   #最后一层是denseblock4的输出
                    gcam = F.conv2d(inter_output, model.classifier.weight.unsqueeze(-1).unsqueeze(-1))
                    gcam = torch.softmax(gcam, dim=-1)
                    pick_label = labels[grade_num + seg_num - model.branch_img_num:grade_num + seg_num]
                    pick_list = []
                    for j in range(pick_label.shape[0]):
                        pick_list.append(gcam[j, pick_label[j]].unsqueeze(0).unsqueeze(0))
                    gcam = torch.cat(pick_list, dim=0)
                else:
                    gcam = torch.sum(inter_gradient * inter_output, dim=1, keepdim=True)

                # 标准化
                """
                gcam_flatten = gcam.view(gcam.shape[0], -1)
                gcam_var = torch.var(gcam_flatten, dim=1).detach()
                gcam = gcam/gcam_var
                gcam = torch.sigmoid(gcam)
                #"""
                """
                pos = torch.gt(gcam, 0).float()
                gcam_pos = gcam * pos
                gcam_neg = gcam * (1 - pos)

                gcam_pos_abs_max = torch.max(gcam_pos.view(gcam.shape[0], -1), dim=1)[0].clamp(1E-12).unsqueeze(
                    -1).unsqueeze(-1).unsqueeze(-1).expand_as(gcam)
                gcam_neg_abs_max = torch.max(gcam_neg.abs().view(gcam.shape[0], -1), dim=1)[0].clamp(1E-12).unsqueeze(
                    -1).unsqueeze(-1).unsqueeze(-1).expand_as(gcam)
                gcam_abs_max = torch.max(gcam.abs().view(gcam.shape[0], -1), dim=1)[0].clamp(1E-12).unsqueeze(
                    -1).unsqueeze(-1).unsqueeze(-1).expand_as(gcam)

                gcam_pos_mean = (torch.sum(gcam_pos) / torch.sum(pos).clamp(min=1E-12))
                gcam_neg_mean = (torch.sum(gcam_neg) / torch.sum(1-pos).clamp(min=1E-12))

                sigma = 1#0.8#0.8
                #gcam = torch.relu(torch.tanh(gcam))
                #gcam = gcam/gcam_abs_max
                gcam = gcam_pos / (gcam_pos_abs_max.clamp(min=1E-12).detach() * sigma) + gcam_neg / gcam_neg_abs_max.clamp(min=1E-12).detach()  # [-1,+1]
                #gcam = (1 - torch.relu(-gcam_pos / (gcam_pos_abs_max.clamp(min=1E-12).detach() * sigma) + 1)) #+ gcam_neg / gcam_neg_abs_max.clamp(min=1E-12).detach()  # cjy
                #gcam = (1 - torch.relu(-gcam_pos / (gcam_pos_mean.clamp(min=1E-12).detach()) + 1)) + gcam_neg / gcam_neg_abs_max.clamp(min=1E-12).detach()
                #gcam = torch.tanh(gcam_pos/gcam_pos_mean.clamp(min=1E-12).detach()) + gcam_neg/gcam_neg_abs_max.clamp(min=1E-12).detach()
                #gcam = gcam_pos / gcam_pos_mean.clamp(min=1E-12).detach() #+ gcam_neg / gcam_neg_mean.clamp(min=1E-12).detach()
                # gcam = gcam/2 + 0.5

                # gcam_max = torch.max(torch.relu(gcam).view(gcam.shape[0], -1), dim=1)[0].clamp(1E-12).unsqueeze(-1).unsqueeze(-1).unsqueeze(-1).expand_as(gcam)
                # gcam_min = torch.min(gcam.view(gcam.shape[0], -1), dim=1)[0].clamp(1E-12).unsqueeze(-1).unsqueeze(-1).unsqueeze(-1).expand_as(gcam)
                # gcam = torch.relu(gcam) / gcam_max.detach()
                # """

                #gcam = torch.tanh(gcam*4)
                # 插值
                gcam = torch.nn.functional.interpolate(gcam, (seg_gt_masks.shape[-2], seg_gt_masks.shape[-1]), mode='bilinear')  #mode='nearest'  'bilinear'
                gcam_list.append(gcam)   #将不同模块的gcam保存到gcam_list中

            # 进行特定的插值
            """
            overall_gcam = torch.cat(gcam_list, dim=1)
            #overall_gcam_index1 = torch.max(overall_gcam, dim=1, keepdim=True)[1]
            #overall_gcam = torch.max(overall_gcam, dim=1, keepdim=True)[0]
            overall_gcam_index = torch.max(overall_gcam.abs(), dim=1, keepdim=True)[1]
            overall_gcam_index_onehot = torch.nn.functional.one_hot(overall_gcam_index.permute(0, 2, 3, 1), target_layer_num).squeeze(3).permute(0, 3, 1, 2)
            #if overall_gcam_index_onehot.shape[1] == 1:
                #overall_gcam_index_onehot = overall_gcam_index_onehot + 1
            overall_gcam = overall_gcam * overall_gcam_index_onehot
            overall_gcam = torch.sum(overall_gcam, dim=1, keepdim=True)
            #overall_gcam = torch.relu(overall_gcam)  # 只保留正值
            #overall_gcam = torch.mean(overall_gcam, dim=1, keepdim=True)
            #overall_gcam = torch.relu(overall_gcam)
            gcam_list = [overall_gcam]
            #"""

            model.inter_output.clear()
            model.inter_gradient.clear()

            if model.gcamSupervisedType == "seg_gtmask":
                gcam_gtmasks = seg_gt_masks
                gcam_labels = seg_labels
                gcam_masks = gcam_list
            elif model.gcamSupervisedType == "seg_mask":
                gcam_gtmasks = seg_masks
                gcam_labels = labels[labels.shape[0]-gcam_gtmasks.shape[0]:labels.shape[0]]
                gcam_masks = gcam_list
            elif model.gcamSupervisedType == "none":
                gcam_masks = None
                gcam_gtmasks = None
                gcam_labels = None
        else:
            gcam_masks = None
            gcam_gtmasks = None
            gcam_labels = None

            # Branch 3 Masked Img Reload
        if model.reloadState == True:
            #(1).生成soft_mask
            if model.maskedImgReloadType == "seg_mask":
                if model.segmentationType != "denseFC":
                    raise Exception("segmentationType can't match maskedImgReloadType")
                soft_mask = seg_masks
                soft_mask = model.lesionFusion(soft_mask, labels[labels.shape[0]-soft_mask.shape[0]:labels.shape[0]])
                #soft_mask = torch.nn.functional.max_pool2d(soft_mask, kernel_size=31, stride=1, padding=15)
            elif model.maskedImgReloadType == "gcam_mask":
                # GAIN论文中 生成soft_mask的做法
                sigma = 0.5
                w = 8
                soft_mask = torch.sigmoid(w * (overall_gcam - sigma))  # overall_gcam [0,1]
            elif model.maskedImgReloadType == "seg_gtmask":
                soft_mask = seg_gt_masks
                soft_mask = model.lesionFusion(soft_mask, labels[labels.shape[0]-soft_mask.shape[0]:labels.shape[0]])
                max_kernel_size = 80#random.randint(30, 160)
                soft_mask = torch.nn.functional.max_pool2d(soft_mask, kernel_size=max_kernel_size*2+1, stride=1, padding=max_kernel_size)
                #soft_mask = torch.nn.functional.avg_pool2d(soft_mask, kernel_size=81, stride=1, padding=40)
                #soft_mask = 1 - soft_mask

                # 模板
                """
                a = torch.Tensor([0.485, 0.456, 0.406])
                b = torch.Tensor([0.229, 0.224, 0.225])
                c = (0-a)/b
                new_soft_mask = (1 - soft_mask).expand(soft_mask.shape[0], 3, soft_mask.shape[2], soft_mask.shape[3])
                c = c.unsqueeze(0).unsqueeze(-1).unsqueeze(-1).expand_as(new_soft_mask).cuda()
                sm = c * new_soft_mask
                #"""
            elif model.maskedImgReloadType == "joint":
                if model.segmentationType != "denseFC":
                    raise Exception("segmentationType can't match maskedImgReloadType")
                #soft_mask = torch.cat([torch.sigmoid(model.base.seg_attention), gcam], dim=1)
                soft_mask = torch.cat([seg_gt_masks, overall_gcam], dim=1)   # 将分割结果替换成真正标签
                soft_mask = torch.max(soft_mask, dim=1, keepdim=True)[0].detach()
                #soft_mask = torch.nn.functional.max_pool2d(soft_mask, kernel_size=501, stride=1, padding=250)
                #soft_mask = torch.nn.functional.avg_pool2d(soft_mask, kernel_size=81, stride=1, padding=40)
            else:
                pass

            # (2).生成masked_img
            rimgs = imgs[imgs.shape[0]-soft_mask.shape[0]:imgs.shape[0]]
            rimg_mean = rimgs.mean(-1, keepdim=True).mean(-2,keepdim=True)
            pos_masked_img = soft_mask * rimgs #+ (1-soft_mask) * rimg_mean
            neg_masked_img = (1-soft_mask) * rimgs #+ soft_mask * rimg_mean

            # (3).reload maskedImg
            # 使用参数相同的网络，但是不回传
            """
            transfer_weights(model, model2)
            model2.eval()
            model2.transimitBatchDistribution(0)
            pm_logits = None#model(pos_masked_img)
            nm_logits = model2(neg_masked_img)
            #"""
            # 使用同一个网络，回传梯度
            #"""
            model.eval()
            model.transimitBatchDistribution(0)
            masked_img = torch.cat([rimgs, pos_masked_img, neg_masked_img], dim=0)
            m_logits = model(masked_img)
            om_logits = m_logits[0:m_logits.shape[0]//3]
            pm_logits = m_logits[m_logits.shape[0]//3 :m_logits.shape[0]//3 * 2]
            nm_logits = m_logits[m_logits.shape[0]//3 * 2:m_logits.shape[0]]
            #pm_logits = model(pos_masked_img)
            #nm_logits = model(neg_masked_img)
            #"""
        else:
            om_logits = None
            pm_logits = None
            nm_logits = None

        # for show loss 计算想查看的loss
        forShow = 0#gcam_pos_abs_max.mean()#gcam_loss_weight

        # 计算loss
        #利用不同的optimizer对模型中的各子模块进行分阶段优化。目前最简单的方式是周期循环启用optimizer
        losses = loss_fn[engine.state.losstype](logit=logits, label=labels, multilabel=one_hot_labels, seg_mask=seg_masks, seg_gtmask=seg_gtmasks, seg_label=seg_labels, gcam_mask=gcam_masks, gcam_gtmask=gcam_gtmasks, gcam_label=gcam_labels, origin_logit=om_logits, pos_masked_logit=pm_logits, neg_masked_logit=nm_logits, show=forShow)    #损失词典
        #为了减少"pos_masked_img_loss" 和 "cross_entropy_loss"之间的冲突，特设定动态weight，使用 "cross_entropy_loss" detach
        #pos_masked_img_loss_weight = 1/(1+losses["cross_entropy_loss"].detach())

        """
        l1 = losses["cross_entropy_loss"]
        l2 = losses["gcam_mask_loss"][0]
        if isinstance(l2, torch.Tensor):
            l1.backward(retain_graph=True)
            for op in optimizers:
                op.zero_grad()
            l2.backward(retain_graph=True)
            for op in optimizers:
                op.zero_grad()

        #"""

        weight = {"cross_entropy_multilabel_loss":1, "cross_entropy_loss":1, "seg_mask_loss":1, "gcam_mask_loss":1, "pos_masked_img_loss":0, "neg_masked_img_loss":0.2, "for_show_loss":0}
        gl_weight = [1, 1, 1, 1]
        loss = 0
        for lossKey in losses.keys():
            if lossKey == "gcam_mask_loss":
                gcam_loss = 0
                for index, gl in enumerate(losses[lossKey]):
                    gcam_loss = gcam_loss + gl * gl_weight[index]
                loss = loss + gcam_loss * weight[lossKey]
            else:
                loss += losses[lossKey] * weight[lossKey]
        loss = loss/model.accumulation_steps

        # 反向传播
        loss.backward()
        # 参数优化
        if engine.state.iteration % model.accumulation_steps == 0:  # 此处要注意
            optimizers[engine.state.optimizer_index].step()
            for op in optimizers:
                op.zero_grad()

        # compute acc
        #acc = (scores.max(1)[1] == grade_labels).float().mean()
        return {"scores": grade_logits, "labels": grade_labels, "losses": losses, "total_loss": loss.item()}
    engine = Engine(_update)

    for name, metric in metrics.items():
        metric.attach(engine, name)

    return engine


def do_train(
        cfg,
        model,
        train_loader,
        val_loader,
        classes_list,
        optimizers,
        schedulers,
        loss_fn,
        start_epoch,
):
    #1.先把cfg中的参数导出
    epochs = cfg.SOLVER.MAX_EPOCHS
    log_period = cfg.SOLVER.LOG_PERIOD
    checkpoint_period = cfg.SOLVER.CHECKPOINT_PERIOD
    eval_period = cfg.SOLVER.EVAL_PERIOD
    output_dir = cfg.SOLVER.OUTPUT_DIR
    device = cfg.MODEL.DEVICE

    #2.构建模块
    logger = logging.getLogger("fundus_prediction.train")
    logger.info("Start training")

    # TensorBoard setup
    writer_train = {}
    for i in range(len(optimizers)):
        writer_train[i] = SummaryWriter(cfg.SOLVER.OUTPUT_DIR + "/summary/train/" + str(i))

    writer_val = SummaryWriter(cfg.SOLVER.OUTPUT_DIR + "/summary/val")

    writer_train["graph"] = SummaryWriter(cfg.SOLVER.OUTPUT_DIR + "/summary/train/graph")

    try:
        #print(model)
        images, labels = next(iter(train_loader))
        grid = torchvision.utils.make_grid(images)
        writer_train["graph"].add_image('images', grid, 0)
        writer_train["graph"].add_graph(model, images)
        writer_train["graph"].flush()
    except Exception as e:
        print("Failed to save model graph: {}".format(e))



    # 设置训练相关的metrics
    metrics_train = {"avg_total_loss": RunningAverage(output_transform=lambda x: x["total_loss"]),
                     "avg_precision": RunningAverage(Precision(output_transform=lambda x: (x["scores"], x["labels"]))),
                     "avg_accuracy": RunningAverage(Accuracy(output_transform=lambda x: (x["scores"], x["labels"]))),  #由于训练集样本均衡后远离原始样本集，故只采用平均metric
                     }

    lossKeys = cfg.LOSS.TYPE.split(" ")
    # 设置loss相关的metrics
    for lossName in lossKeys:
        if lossName == "similarity_loss":
            metrics_train["AVG-" + "similarity_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["similarity_loss"])
        elif lossName == "ranked_loss":
            metrics_train["AVG-" + "ranked_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["ranked_loss"])
        elif lossName == "cranked_loss":
            metrics_train["AVG-" + "cranked_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["cranked_loss"])
        elif lossName == "cross_entropy_loss":
            metrics_train["AVG-" + "cross_entropy_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["cross_entropy_loss"])
        elif lossName == "cluster_loss":
            metrics_train["AVG-" + "cluster_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["cluster_loss"][0])
        elif lossName == "one_vs_rest_loss":
            metrics_train["AVG-" + "one_vs_rest_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["one_vs_rest_loss"])
        elif lossName == "attention_loss":
            metrics_train["AVG-" + "attention_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["attention_loss"])
        elif lossName == "class_predict_loss":
            metrics_train["AVG-" + "class_predict_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["class_predict_loss"])
        elif lossName == "kld_loss":
            metrics_train["AVG-" + "kld_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["kld_loss"])
        elif lossName == "margin_loss":
            metrics_train["AVG-" + "margin_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["margin_loss"])
        elif lossName == "cross_entropy_multilabel_loss":
            metrics_train["AVG-" + "cross_entropy_multilabel_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["cross_entropy_multilabel_loss"])
        elif lossName == "seg_mask_loss":
            metrics_train["AVG-" + "seg_mask_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["seg_mask_loss"])
        elif lossName == "gcam_mask_loss":
            #metrics_train["AVG-" + "gcam_mask_loss"] = RunningAverage(
            #    output_transform=lambda x: x["losses"]["gcam_mask_loss"])
            metrics_train["AVG-" + "gcam_mask_loss0"] = RunningAverage(
                output_transform=lambda x: x["losses"]["gcam_mask_loss"][0])
            metrics_train["AVG-" + "gcam_mask_loss1"] = RunningAverage(
                output_transform=lambda x: x["losses"]["gcam_mask_loss"][1])
            metrics_train["AVG-" + "gcam_mask_loss2"] = RunningAverage(
                output_transform=lambda x: x["losses"]["gcam_mask_loss"][2])
            metrics_train["AVG-" + "gcam_mask_loss3"] = RunningAverage(
                output_transform=lambda x: x["losses"]["gcam_mask_loss"][3])
        elif lossName == "pos_masked_img_loss":
            metrics_train["AVG-" + "pos_masked_img_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["pos_masked_img_loss"])
        elif lossName == "neg_masked_img_loss":
            metrics_train["AVG-" + "neg_masked_img_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["neg_masked_img_loss"])
        elif lossName == "for_show_loss":
            metrics_train["AVG-" + "for_show_loss"] = RunningAverage(
                output_transform=lambda x: x["losses"]["for_show_loss"])
        else:
            raise Exception('expected METRIC_LOSS_TYPE should be similarity_loss, ranked_loss, cranked_loss'
                            'but got {}'.format(cfg.LOSS.TYPE))


    trainer = create_supervised_trainer(model, optimizers, metrics_train, loss_fn, device=device,)

    #CJY  at 2019.9.26
    def output_transform(output):
        # `output` variable is returned by above `process_function`
        y_pred = output['scores']
        y = output['labels']
        return y_pred, y  # output format is according to `Accuracy` docs

    metrics_eval = {"overall_accuracy": Accuracy(output_transform=output_transform),
                    "precision": Precision(output_transform=output_transform)}

    checkpointer = ModelCheckpoint(output_dir, cfg.MODEL.NAME, checkpoint_period, n_saved=300, require_empty=False, start_step=start_epoch)
    #checkpointer_save_graph = ModelCheckpoint(output_dir, cfg.MODEL.NAME+"_graph", checkpoint_period, n_saved=300, require_empty=False, start_step=start_epoch, save_as_state_dict=False)
    timer = Timer(average=True)

    #3.将模块与engine联系起来attach
    #CJY at 2019.9.23
    trainer.add_event_handler(Events.EPOCH_COMPLETED, checkpointer, {'model': model,
                                                                     'optimizer': optimizers[0]})

    #trainer.add_event_handler(Events.STARTED, checkpointer, {'model': model,
    #                                                                 'optimizer': optimizers[0]})
    #trainer.add_event_handler(Events.STARTED, checkpointer_save_graph, {'model': model,
    #                                                                 'optimizer': optimizers[0]})
    #torch.save(model, output_dir + "/" + cfg.MODEL.NAME+"_graph.pkl")

    timer.attach(trainer, start=Events.EPOCH_STARTED, resume=Events.ITERATION_STARTED,
                 pause=Events.ITERATION_COMPLETED, step=Events.ITERATION_COMPLETED)


    #4.事件处理函数
    @trainer.on(Events.STARTED)
    def start_training(engine):
        engine.state.epoch = start_epoch
        engine.state.iteration = engine.state.iteration + start_epoch * len(train_loader)

        logger.info("Model:{}".format(model))
        logger.info("Model:{}".format(model.count_param()))
        inputshape = (3, cfg.DATA.TRANSFORM.SIZE[0], cfg.DATA.TRANSFORM.SIZE[1])
        logger.info("Model:{}".format(model.count_param2(input_shape=inputshape)))
        #print(model)
        #print(model.count_param())
        #print(model.count_param2())

        """
        metrics = do_inference(cfg, model, val_loader, classes_list, loss_fn, plotFlag=False)

        step = 0#len(train_loader) * (engine.state.epoch - 1) + engine.state.iteration
        for preKey in metrics['precision'].keys():
            writer_val.add_scalar("Precision/" + str(preKey), metrics['precision'][preKey], step)

        for recKey in metrics['recall'].keys():
            writer_val.add_scalar("Recall/" + str(recKey), metrics['recall'][recKey], step)

        for aucKey in metrics['roc_auc'].keys():
            writer_val.add_scalar("ROC_AUC/" + str(aucKey), metrics['roc_auc'][aucKey], step)

        writer_val.add_scalar("OverallAccuracy", metrics["overall_accuracy"], step)

        # writer.add_scalar("Val/"+"confusion_matrix", metrics['confusion_matrix'], step)

        # 混淆矩阵 和 ROC曲线可以用图的方式来存储
        roc_numpy = metrics["roc_figure"]
        writer_val.add_image("ROC", roc_numpy, step, dataformats='HWC')

        confusion_matrix_numpy = metrics["confusion_matrix_numpy"]
        writer_val.add_image("ConfusionMatrix", confusion_matrix_numpy, step, dataformats='HWC')

        writer_val.flush()
        #"""

    @trainer.on(Events.EPOCH_COMPLETED) #_STARTED)   #注意，在pytorch1.2里面 scheduler.steo()应该放到 optimizer.step()之后
    def adjust_learning_rate(engine):
        """
        #if (engine.state.epoch - 1) % engine.state.epochs_traverse_optimizers == 0:
        if engine.state.epoch == 2:
            op_i_scheduler1 = WarmupMultiStepLR(optimizers[0], cfg.SOLVER.SCHEDULER.STEPS, cfg.SOLVER.SCHEDULER.GAMMA,
                                               cfg.SOLVER.SCHEDULER.WARMUP_FACTOR,
                                               cfg.SOLVER.SCHEDULER.WARMUP_ITERS, cfg.SOLVER.SCHEDULER.WARMUP_METHOD)
            op_i_scheduler2 = WarmupMultiStepLR(optimizers[1], cfg.SOLVER.SCHEDULER.STEPS, cfg.SOLVER.SCHEDULER.GAMMA,
                                                cfg.SOLVER.SCHEDULER.WARMUP_FACTOR,
                                                cfg.SOLVER.SCHEDULER.WARMUP_ITERS, cfg.SOLVER.SCHEDULER.WARMUP_METHOD)
            engine.state.schedulers = [op_i_scheduler1, op_i_scheduler2]
            print("copy")
        """
        schedulers[engine.state.schedulers_epochs_index][engine.state.optimizer_index].step()

    @trainer.on(Events.ITERATION_COMPLETED)
    def log_training_loss(engine):
        global ITER
        ITER += 1

        if ITER % (log_period*model.accumulation_steps) == 0:
            step = engine.state.iteration

            #写入train-summary
            #记录avg-presicion
            avg_precision = engine.state.metrics['avg_precision'].numpy().tolist()
            avg_precisions = {}
            ap_sum = 0
            for index, ap in enumerate(avg_precision):
                avg_precisions[index] = float("{:.2f}".format(ap))
                ap_sum += avg_precisions[index]
                scalarDict = {}
                for i in range(len(optimizers)):
                    if i != engine.state.optimizer_index:
                        scalarDict["optimizer" + str(i)] = 0
                    else:
                        scalarDict["optimizer" + str(i)] = avg_precisions[index]
                    writer_train[i].add_scalar("Precision/" + str(index), scalarDict["optimizer" + str(i)], step)
                    writer_train[i].flush()
            avg_precisions["avg_precision"] = float("{:.2f}".format(ap_sum/len(avg_precision)))

            #记录avg-loss
            avg_losses = {}
            for lossName in lossKeys:
                if lossName == "gcam_mask_loss":
                    for i in range(4):
                        avg_losses[lossName+str(i)] = (float("{:.3f}".format(engine.state.metrics["AVG-" + lossName + str(i)])))
                        scalarDict = {}
                        for j in range(len(optimizers)):
                            if j != engine.state.optimizer_index:
                                scalarDict["optimizer" + str(j)] = 0
                            else:
                                scalarDict["optimizer" + str(j)] = avg_losses[lossName+str(i)]
                            writer_train[j].add_scalar("Loss/" + lossName + str(i), scalarDict["optimizer" + str(j)], step)
                            writer_train[j].flush()
                else:
                    avg_losses[lossName] = (float("{:.3f}".format(engine.state.metrics["AVG-" + lossName])))
                    scalarDict = {}
                    for i in range(len(optimizers)):
                        if i != engine.state.optimizer_index:
                            scalarDict["optimizer" + str(i)] = 0
                        else:
                            scalarDict["optimizer" + str(i)] = avg_losses[lossName]
                        writer_train[i].add_scalar("Loss/" + lossName, scalarDict["optimizer" + str(i)], step)
                        writer_train[i].flush()


            #记录其余标量
            scalar_list = ["avg_accuracy", "avg_total_loss"]
            for scalar in scalar_list:
                scalarDict = {}
                for i in range(len(optimizers)):
                    if i != engine.state.optimizer_index:
                        scalarDict["optimizer" + str(i)] = 0
                    else:
                        scalarDict["optimizer" + str(i)] = engine.state.metrics[scalar]
                    writer_train[i].add_scalar("Train/" + scalar, scalarDict["optimizer" + str(i)], step)
                    writer_train[i].flush()

            #记录学习率
            LearningRateDict = {}
            for i in range(len(optimizers)):
                if i != engine.state.optimizer_index:
                    LearningRateDict["optimizer" + str(i)] = 0
                else:
                    LearningRateDict["optimizer" + str(i)] = schedulers[engine.state.schedulers_epochs_index][engine.state.optimizer_index].get_lr()[0]
                writer_train[i].add_scalar("Train/" + "LearningRate", LearningRateDict["optimizer" + str(i)], step)
                writer_train[i].flush()

            #记录weight
            choose_list = ["base.conv1.weight", "base.bn1.weight",
                          "base.layer1.0.conv1.weight", "base.layer1.2.conv3.weight",
                          "base.layer2.0.conv1.weight", "base.layer2.3.conv3.weight",
                          "base.layer3.0.conv1.weight", "base.layer3.5.conv3.weight",
                          "base.layer4.0.conv1.weight", "base.layer4.2.conv1.weight",
                          "bottleneck.weight", "classifier.weight"]
            """
            #记录参数分布 非常耗时
            params_dict = {}
            for name, parameters in model.named_parameters():
                #print(name, ':', parameters.size())
                params_dict[name] = parameters.detach().cpu().numpy()
            #print(len(params_dict))
                        
            for cp in params_dict.keys():
                writer_train["graph"].add_histogram("Train/" + cp, params_dict[cp], step)
                writer_train["graph"].flush()
            #"""

            logger.info("Epoch[{}] Iteration[{}/{}] Training {} - ATLoss: {:.3f}, AvgLoss: {}, Avg Pre: {}, Avg_Acc: {:.3f}, Base Lr: {:.2e}, step: {}"
                        .format(engine.state.epoch, ITER, len(train_loader),
                                engine.state.losstype,
                                engine.state.metrics['avg_total_loss'], avg_losses, avg_precisions, engine.state.metrics['avg_accuracy'],
                                schedulers[engine.state.schedulers_epochs_index][engine.state.optimizer_index].get_lr()[0], step))

            #logger.info(engine.state.output["rf_loss"])

            if engine.state.output["losses"].get("cluster_loss") != None:
                logger.info("Epoch[{}] Iteration[{}/{}] Center {} \n r_inter: {}, r_outer: {}, step: {}"
                            .format(engine.state.epoch, ITER, len(train_loader),
                                    engine.state.output["losses"]["cluster_loss"][-1]["center"].cpu().detach().numpy(),
                                    engine.state.output["losses"]["cluster_loss"][-1]["r_inter"].item(),
                                    engine.state.output["losses"]["cluster_loss"][-1]["r_outer"].item(),
                                    step))

        if len(train_loader) == ITER:
            ITER = 0

    # adding handlers using `trainer.on` decorator API
    @trainer.on(Events.EPOCH_COMPLETED)
    def print_times(engine):
        logger.info('Epoch {} done. Time per batch: {:.3f}[s] Speed: {:.1f}[samples/s]'
                    .format(engine.state.epoch, timer.value() * timer.step_count,
                            train_loader.batch_size / timer.value()))
        logger.info('-' * 10)
        timer.reset()

    @trainer.on(Events.EPOCH_COMPLETED)
    def log_validation_results(engine):
        if engine.state.epoch % eval_period == 0:
            metrics = do_inference(cfg, model, val_loader, classes_list, loss_fn)

            step = engine.state.iteration
            for preKey in metrics['precision'].keys():
                writer_val.add_scalar("Precision/" + str(preKey), metrics['precision'][preKey], step)

            for recKey in metrics['recall'].keys():
                writer_val.add_scalar("Recall/" + str(recKey), metrics['recall'][recKey], step)

            #for aucKey in metrics['roc_auc'].keys():
            #    writer_val.add_scalar("ROC_AUC/" + str(aucKey), metrics['roc_auc'][aucKey], step)

            writer_val.add_scalar("OverallAccuracy", metrics["overall_accuracy"], step)

            #writer.add_scalar("Val/"+"confusion_matrix", metrics['confusion_matrix'], step)

            #混淆矩阵 和 ROC曲线可以用图的方式来存储
            #roc_numpy = metrics["roc_figure"]
            #writer_val.add_image("ROC", roc_numpy, step, dataformats='HWC')

            #confusion_matrix_numpy = metrics["confusion_matrix_numpy"]
            #writer_val.add_image("ConfusionMatrix", confusion_matrix_numpy, step, dataformats='HWC')

            writer_val.flush()


    #5.engine运行
    trainer.run(train_loader, max_epochs=epochs)
    for key in writer_train.keys():
        writer_train[key].close()
    writer_val.close()

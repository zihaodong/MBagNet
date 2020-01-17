# encoding: utf-8
"""
@author:  zzg
@contact: xhx1247786632@gmail.com
"""
import torch
from torch import nn
import torch.nn.functional as F


def normalize_rank(x, axis=-1):
    """Normalizing to unit length along the specified dimension.
    Args:
      x: pytorch Variable
    Returns:
      x: pytorch Variable, same shape as input
    """
    x = 1. * x / (torch.norm(x, 2, axis, keepdim=True).expand_as(x) + 1e-12)
    return x


def euclidean_dist_rank(x, y):
    """
    Args:
      x: pytorch Variable, with shape [m, d]
      y: pytorch Variable, with shape [n, d]
    Returns:
      dist: pytorch Variable, with shape [m, n]
    """
    m, n = x.size(0), y.size(0)
    xx = torch.pow(x, 2).sum(1, keepdim=True).expand(m, n)
    yy = torch.pow(y, 2).sum(1, keepdim=True).expand(n, m).t()
    dist = xx + yy
    dist.addmm_(1, -2, x, y.t())
    dist = dist.clamp(min=1e-12).sqrt()  # for numerical stability
    return dist


def rank_loss(dist_mat, labels, margin, alpha, tval):
    """
    Args:
      dist_mat: pytorch Variable, pair wise distance between samples, shape [N, N]
      labels: pytorch LongTensor, with shape [N]

    """
    assert len(dist_mat.size()) == 2
    assert dist_mat.size(0) == dist_mat.size(1)
    N = dist_mat.size(0)

    total_loss = 0.0
    for ind in range(N):
        is_pos = labels.eq(labels)
        is_pos[ind] = 0  # 对角线置0
        is_neg = labels.ne(labels)

        dist_ap = dist_mat[ind][is_pos]
        dist_an = dist_mat[ind][is_neg]

        ap_is_pos = torch.clamp(torch.add(dist_ap, margin - alpha), min=0.0)
        ap_pos_num = ap_is_pos.size(0) + 1e-5
        ap_pos_val_sum = torch.sum(ap_is_pos)
        loss_ap = torch.div(ap_pos_val_sum, float(ap_pos_num))

        """
        an_is_pos = torch.lt(dist_an,alpha)
        an_less_alpha = dist_an[an_is_pos]
        an_weight = torch.exp(tval*(-1*an_less_alpha+alpha))
        an_weight_sum = torch.sum(an_weight) +1e-5
        an_dist_lm = alpha - an_less_alpha
        an_ln_sum = torch.sum(torch.mul(an_dist_lm,an_weight))
        loss_an = torch.div(an_ln_sum,an_weight_sum)
        """

        total_loss = total_loss + loss_ap
    total_loss = total_loss * 1.0 / N
    return total_loss


"""
# loss = -熵
class CommonLoss(object):
    "Common_Loss_for_Deep_Metric_Learning_CVPR_2019_paper"

    def __init__(self, num_classes, margin=None, alpha=None, tval=None):
        self.margin = margin
        #self.alpha = alpha
        #self.tval = tval
        cuda0 = torch.device('cuda:0')
        self.vector_label = torch.tensor([1/num_classes], dtype=torch.float64, device=cuda0)
        print(self.vector_label)

    def __call__(self, scores, labels):
        #if normalize_feature:
        #    global_feat = normalize_rank(global_feat, axis=-1)

        score_softmax = F.softmax(scores, dim=1)
        score_logsoftmax = F.log_softmax(scores, dim=1)
        score_preH = score_logsoftmax.mul(score_softmax)
        common_loss = torch.mean(score_preH)  #  dim1在上求得每个样本的熵，在dim0上求得batch的   x(-1)   均匀分布时熵最大

        return common_loss
"""


class SimilarityLoss(object):
    "Ranked_List_Loss_for_Deep_Metric_Learning_CVPR_2019_paper"

    def __init__(self):

        pass


    def __call__(self, similarities, similiarity_labels, multilabel):

        #计算每一维特征之间的关系  1024维
        s1 = similarities.unsqueeze(2).expand(similarities.shape[0], similarities.shape[1], similarities.shape[1], similarities.shape[2])
        s2 = s1.permute(0, 2, 1, 3)
        loss = torch.nn.functional.cosine_similarity(s1, s2, dim=-1, eps=1e-12)



        #loss = torch.tril(loss)
        eyeMat = torch.eye(loss.shape[1], loss.shape[2]).unsqueeze(0).expand_as(loss).bool()
        loss[eyeMat] = 0

        loss_mean = torch.mean(torch.abs(loss), dim=-1)

        loss_max = torch.max(torch.abs(loss), dim=-1)

        """
        import seaborn as sns
        import matplotlib.pyplot as plt
        lossAbs = torch.abs(loss)
        for i in range(loss.shape[0]):
            data = lossAbs[i].cpu().detach().numpy()
            heatmap = sns.heatmap(data)
            plt.show()
        #"""

        total_loss = torch.max(torch.abs(loss))

        #mean_loss = torch.nn.functional.adaptive_max_pool2d(abs(loss), (1, loss.shape[3]))

        #CJY 计算每个rf的cross-entropy-loss并求均值。
        """
        #batch = similarities.shape[0]
        labels = similiarity_labels.unsqueeze(-1).expand(similarities.shape[0], similarities.shape[2])
        loss = F.cross_entropy(similarities, labels, reduction="none")
        #loss_score = torch.softmax(loss, dim=-1)
        loss = loss * multilabel
        total_loss = torch.mean(loss)

        #"""

        #CJY 计算每对rf特征之间的相关性
        """
        #s0 = similarities.unsqueeze(2)
        s1 = similarities.unsqueeze(2).expand(similarities.shape[0], similarities.shape[1], similarities.shape[2], similarities.shape[2], similarities.shape[3])
        s2 = s1.permute(0, 1, 3, 2, 4)
        loss = torch.nn.functional.cosine_similarity(s1, s2, dim=-1, eps=1e-12)

        loss = torch.tril(loss)
        eyeMat = torch.eye(loss.shape[2], loss.shape[3]).unsqueeze(0).unsqueeze(0).expand_as(loss).bool()
        loss[eyeMat] = 0

        mean_loss = torch.nn.functional.adaptive_max_pool2d(abs(loss), (1, loss.shape[3]))
        #"""
        """
        import seaborn as sns
        import matplotlib.pyplot as plt
        for i in range(loss.shape[1]):
            data = loss[0][i].cpu().detach().numpy()
            heatmap = sns.heatmap(data)
            plt.show()
        total_loss = torch.mean(mean_loss)
        """
        #"""


        #CJY 计算。
        """
        s = torch.sum(torch.abs(similarities), dim=1)
        s1 = s/torch.sum(s, dim=-1, keepdim=True)

        labels = similiarity_labels.unsqueeze(-1).expand(similarities.shape[0], similarities.shape[2])
        l = F.cross_entropy(similarities, labels, reduction="none")

        loss = torch.sum(s1 * multilabel * l, dim=-1)
        total_loss = torch.mean(loss)
        #"""

        #直接将
        #total_loss = similarities

        return total_loss


import re
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as cp
from collections import OrderedDict
from .utils import load_state_dict_from_url

from ..plagins.non_local import NonLocal2D
from ..plagins.context_block import ContextBlock

#from ..utils.orthogonal_conv import Conv2d
from torch.nn import Conv2d
from utils import featrueVisualization as fV


__all__ = ['MultiBagNet', 'mbagnet224', 'mbagnet121', 'mbagnet169', 'mbagnet201', 'mbagnet161']

model_urls = {
    'densenet121': 'https://download.pytorch.org/models/densenet121-a639ec97.pth',
    'densenet169': 'https://download.pytorch.org/models/densenet169-b2777c0a.pth',
    'densenet201': 'https://download.pytorch.org/models/densenet201-c1103571.pth',
    'densenet161': 'https://download.pytorch.org/models/densenet161-8d451a50.pth',
}


#CJY 计算不同大小，不同位置的感受野提供的logits以及可视化



def _bn_function_factory(norm, relu, conv, preAct=True):
    if preAct == True:
        def bn_function(*inputs):
            concated_features = torch.cat(inputs, 1)
            bottleneck_output = conv(relu(norm(concated_features)))
            return bottleneck_output
    else:
        def bn_function(*inputs):
            concated_features = torch.cat(inputs, 1)
            bottleneck_output = relu(norm(conv(concated_features)))#conv(relu(norm(concated_features)))
            return bottleneck_output

    return bn_function

class _MBagLayer(nn.Sequential):
    def __init__(self, num_input_features, growth_rate, bn_size, drop_rate, memory_efficient=False, preAct=True, reduction=1):
        super(_MBagLayer, self).__init__()

        self.preAct = preAct
        self.reduction = reduction

        if self.preAct == True:
            self.add_module('norm1', nn.BatchNorm2d(num_input_features)),   #改变norm
            self.add_module('relu1', nn.ReLU(inplace=True)),
            self.add_module('conv1', Conv2d(num_input_features, bn_size *
                                            growth_rate, kernel_size=1, stride=1,
                                            bias=False)),
            self.add_module('norm2', nn.BatchNorm2d(bn_size * growth_rate)),
            self.add_module('relu2', nn.ReLU(inplace=True)),
            self.add_module('conv2', Conv2d(bn_size * growth_rate, growth_rate,
                                            kernel_size=3, stride=1, padding=1,
                                            bias=False)),
        else:
            self.add_module('conv1', Conv2d(num_input_features, bn_size *
                                            growth_rate, kernel_size=1, stride=1,
                                            bias=False)),
            self.add_module('norm1', nn.BatchNorm2d(bn_size * growth_rate)),  # 改变norm
            self.add_module('relu1', nn.ReLU(inplace=True)),
            self.add_module('conv2', Conv2d(bn_size * growth_rate, growth_rate,
                                            kernel_size=3, stride=1, padding=1,
                                            bias=False)),
            self.add_module('norm2', nn.BatchNorm2d(growth_rate)),
            self.add_module('relu2', nn.ReLU(inplace=True)),

        if reduction == 1:
            self.add_module('reduction1', nn.Identity())#nn.AvgPool2d(kernel_size=1, stride=1))
        elif reduction > 1:
            self.add_module('reduction1', Conv2d(growth_rate, growth_rate//reduction,
                                            kernel_size=1, stride=1, padding=0,
                                            bias=False)),


        self.drop_rate = drop_rate
        self.memory_efficient = memory_efficient

    def forward(self, *prev_features):
        bn_function = _bn_function_factory(self.norm1, self.relu1, self.conv1, self.preAct)
        if self.memory_efficient and any(prev_feature.requires_grad for prev_feature in prev_features):
            bottleneck_output = cp.checkpoint(bn_function, *prev_features)
        else:
            bottleneck_output = bn_function(*prev_features)

        if self.preAct == True:
            new_features = self.conv2(self.relu2(self.norm2(bottleneck_output)))
        else:
            new_features = self.relu2(self.norm2(self.conv2(bottleneck_output)))

        if self.reduction == 1:    #如果有降维参数，那么可以提前降维
            new_features = self.reduction1(new_features)
        elif self.reduction > 1:
            new_features = self.reduction1(new_features)

        if self.drop_rate > 0:
            new_features = F.dropout(new_features, p=self.drop_rate,
                                     training=self.training)
        return new_features



class _MBagBlock(nn.Module):
    def __init__(self, num_layers, num_input_features, bn_size, growth_rate, drop_rate, memory_efficient=False, preAct=True, fusion="concat", reduction=1):
        super(_MBagBlock, self).__init__()

        self.preAct = preAct
        self.fusion = fusion  # "add"  "concat"
        self.reduction = reduction   #最后结果降维程度
        self.out_channels = num_input_features

        if num_input_features != growth_rate//reduction and self.fusion == "add":
            raise Exception("For add fusion type, num_input_features must equal with growth_rate!")

        for i in range(num_layers):
            layer = _MBagLayer(
                self.out_channels,
                growth_rate=growth_rate,
                bn_size=bn_size,
                drop_rate=drop_rate,
                memory_efficient=memory_efficient,
                preAct=self.preAct,
                reduction=self.reduction
            )
            if self.fusion == "concat":
                self.out_channels = self.out_channels + growth_rate//self.reduction
            elif self.fusion == "add":
                self.out_channels = self.out_channels + 0

            self.add_module('denselayer%d' % (i + 1), layer)


    def forward(self, init_features):
        features = [init_features]
        for name, layer in self.named_children():
            new_features = layer(*features)
            if self.fusion == "concat":
                features.append(new_features)
            elif self.fusion == "add":
                features[0] = features[0] + new_features

        out = torch.cat(features, 1)
        return out


class _Transition(nn.Sequential):
    def __init__(self, num_input_features, num_output_features, num_groups):
        super(_Transition, self).__init__()
        #self.add_module('norm', nn.BatchNorm2d(num_input_features))
        #self.add_module('relu', nn.ReLU(inplace=True))
        #self.add_module('conv', Conv2d(num_input_features, num_output_features, groups=num_groups,  #cjy 新增groups=num_output_features
        #                                  kernel_size=1, stride=1, bias=False))
        # transition 主要负责降分辨率，同时也可负责感受野之间的加权降维（非group的conv）
        # 降低分辨率的方式：1.pool 2.conv(group)不打乱感受野
        self.add_module('pool', nn.AvgPool2d(kernel_size=2, stride=2))



class MultiBagNet(nn.Module):
    r"""Densenet-BC model class, based on
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_

    Args:
        growth_rate (int) - how many filters to add each layer (`k` in paper)
        block_config (list of 4 ints) - how many layers in each pooling block
        num_init_features (int) - the number of filters to learn in the first convolution layer
        bn_size (int) - multiplicative factor for number of bottle neck layers
          (i.e. bn_size * k features in the bottleneck layer)
        drop_rate (float) - dropout rate after each dense layer
        num_classes (int) - number of classification classes
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_
    """

    def __init__(self, growth_rate=32, block_config=(6, 12, 24, 16),
                 num_init_features=32, bn_size=2, drop_rate=0, num_classes=1000, memory_efficient=False, preAct=True, fusion="concat", reduction=1):

        super(MultiBagNet, self).__init__()

        self.preAct = preAct
        self.fusion = fusion
        self.reduction = reduction
        self.num_layers = list(block_config)

        # First convolution
        self.features = nn.Sequential(OrderedDict([
            ('conv0', Conv2d(3, num_init_features, kernel_size=7, stride=2,
                                padding=3, bias=False)),
            ('norm0', nn.BatchNorm2d(num_init_features)),
            ('relu0', nn.ReLU(inplace=True)),
            #('pool0', nn.MaxPool2d(kernel_size=3, stride=2, padding=1)),
        ]))
        self.num_features = num_init_features
        self.num_receptive_field = 1

        if self.reduction == 1:
            self.features.add_module('reduction0', nn.Identity())#(kernel_size=1, stride=1))
        if self.reduction > 1:
            self.features.add_module('reduction0', Conv2d(self.num_features, self.num_features//reduction,
                                                         kernel_size=1, stride=1, padding=0,
                                                         bias=False))
            self.num_features = self.num_features//reduction

        # Each denseblock
        for i, num_layers in enumerate(block_config):
            block = _MBagBlock(
                num_layers=num_layers,
                num_input_features=self.num_features,
                bn_size=bn_size,
                growth_rate=growth_rate,
                drop_rate=drop_rate,
                memory_efficient=memory_efficient,
                fusion=self.fusion,
                reduction=self.reduction
            )
            self.features.add_module('denseblock%d' % (i + 1), block)
            self.num_features = block.out_channels
            self.num_receptive_field = self.num_receptive_field + num_layers

            if i != len(block_config) - 1:
                trans = _Transition(num_input_features=self.num_features,
                                    num_output_features=self.num_features, num_groups=self.num_receptive_field)
                self.features.add_module('transition%d' % (i + 1), trans)


        # CJY 原论文中没有最后的 norm 和 relu
        # Final batch norm
        #self.features.add_module('norm5', nn.BatchNorm2d(num_features))

        # Linear layer
        #self.classifier = nn.Linear(num_features, num_classes)

        self.calculateRF()

        # Official init from torch repo.
        for name, m in self.named_modules():
            if isinstance(m, Conv2d):
                nn.init.kaiming_normal_(m.weight)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.constant_(m.bias, 0)


    def calculateRF(self):
        # 记录下每个感受野的size，stride等参数
        self.receptive_field_list = []
        rf_size = 1
        rf_stride = 1
        rf_padding = 0
        modules = self.named_modules()
        for name, module in modules:
            if isinstance(module, nn.Conv2d):
                if "conv0" in name or "conv2" in name:
                    kernel_size = module.kernel_size[0]
                    stride = module.stride[0]
                    padding = module.padding[0]
                    rf_size = kernel_size * rf_stride + rf_size - rf_stride
                    rf_padding = padding * rf_stride + rf_padding
                    rf_stride = stride * rf_stride
                    self.receptive_field_list.append({"rf_size": rf_size, "rf_stride": rf_stride, "padding": rf_padding})
                    #print(name)
                    #print({"rf_size": rf_size, "rf_stride": rf_stride, "padding": rf_padding})
            if isinstance(module, nn.AvgPool2d):
                kernel_size = module.kernel_size
                stride = module.stride
                padding = module.padding
                rf_size = kernel_size * rf_stride + rf_size - rf_stride
                rf_padding = padding * rf_stride + rf_padding
                rf_stride = stride * rf_stride
        #print(self.receptive_field_list)



    def forward(self, x):
        features = self.features(x)
        #out = F.relu(features, inplace=True)
        #out = F.adaptive_avg_pool2d(out, (1, 1))
        #out = torch.flatten(out, 1)
        #out = self.classifier(out)
        return features


def _load_state_dict(model, model_url, progress):
    # '.'s are no longer allowed in module names, but previous _DenseLayer
    # has keys 'norm.1', 'relu.1', 'conv.1', 'norm.2', 'relu.2', 'conv.2'.
    # They are also in the checkpoints in model_urls. This pattern is used
    # to find such keys.
    pattern = re.compile(
        r'^(.*denselayer\d+\.(?:norm|relu|conv))\.((?:[12])\.(?:weight|bias|running_mean|running_var))$')

    state_dict = load_state_dict_from_url(model_url, progress=progress)
    for key in list(state_dict.keys()):
        res = pattern.match(key)
        if res:
            new_key = res.group(1) + res.group(2)
            state_dict[new_key] = state_dict[key]
            del state_dict[key]
    model.load_state_dict(state_dict)


def _mbagnet(arch, growth_rate, block_config, num_init_features, pretrained, progress,
             **kwargs):
    model = MultiBagNet(growth_rate, block_config, num_init_features, **kwargs)
    if pretrained:
        _load_state_dict(model, model_urls[arch], progress)
    return model


def mbagnet224(pretrained=False, progress=True, **kwargs):
    r"""Densenet-121 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_
    """
    return _mbagnet('mbagnet224', 32, (6, 8, 8), 32, pretrained, progress,
                    **kwargs)

def mbagnet121(pretrained=False, progress=True, **kwargs):
    r"""Densenet-121 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_
    """
    return _mbagnet('mbagnet121', 32, (6, 12, 24, 16), 64, pretrained, progress,
                    **kwargs)


def mbagnet161(pretrained=False, progress=True, **kwargs):
    r"""Densenet-161 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_
    """
    return _mbagnet('mbagnet161', 48, (6, 12, 36, 24), 96, pretrained, progress,
                    **kwargs)


def mbagnet169(pretrained=False, progress=True, **kwargs):
    r"""Densenet-169 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_
    """
    return _mbagnet('mbagnet169', 32, (6, 12, 32, 32), 64, pretrained, progress,
                    **kwargs)


def mbagnet201(pretrained=False, progress=True, **kwargs):
    r"""Densenet-201 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_
    """
    return _mbagnet('mbagnet201', 32, (6, 12, 48, 32), 64, pretrained, progress,
                    **kwargs)

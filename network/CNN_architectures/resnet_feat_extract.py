import torch
import torch.nn as nn
from network.CNN_architectures.pytorch_resnet import block


class ResNet_feat_extract(nn.Module):
    def __init__(self, block=block, layers=[3, 4, 6, 3], image_channels=3):
        super(ResNet_feat_extract, self).__init__()
        self.in_channels = 64



        self.conv1 = nn.Conv2d(image_channels, 64, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU()
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        # Essentially the entire ResNet architecture are in these 4 lines below
        self.layer1 = self._make_layer(
            block, layers[0], intermediate_channels=64, stride=1
        )
        self.layer2 = self._make_layer(
            block, layers[1], intermediate_channels=128, stride=2
        )
        self.layer3 = self._make_layer(
            block, layers[2], intermediate_channels=256, stride=2
        )
        self.layer4 = self._make_layer(
            block, layers[3], intermediate_channels=512, stride=2
        )

        self.fc_feat_extract = nn.Linear(512 * 4, 1024)
        self.fc_feat_extract_1 = nn.Linear(512 * 4, 1024)

        ## anchor prediction
        self.layer3_1 = self._make_layer(
            block, layers[2], intermediate_channels=256, stride=2, in_channel=512
        )
        self.layer4_1 = self._make_layer(
            block, layers[3], intermediate_channels=512, stride=2
        )


        self.fc_classification = nn.Linear(1024, 1)
        self.embedding = nn.Parameter(torch.ones(1, 1024))
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.avgpool_1 = nn.AdaptiveAvgPool2d((1, 1))
        self.triplet_loss = nn.TripletMarginLoss(margin=1.0, p=1)
        self.l1_loss = nn.SmoothL1Loss()
        self.bce_loss = nn.BCEWithLogitsLoss()
        self.consine_similarity = nn.CosineSimilarity(dim=1, eps=1e-6)

    def feat_extract(self, x):

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)
        x = self.layer1(x)
        x = self.layer2(x)

        y = self.layer3(x.clone())
        y = self.layer4(y)
        y = self.avgpool(y)
        y = y.reshape(y.shape[0], -1)
        y = self.fc_feat_extract(y)

        ## anchor prediction
        y_anch = self.layer3_1(x)
        y_anch = self.layer4_1(y_anch)
        y_anch = self.avgpool_1(y_anch)
        y_anch = y_anch.reshape(y_anch.shape[0], -1)
        y_anch = self.fc_feat_extract_1(y_anch)

        return y, y_anch

    def forward(self, attacked_positive, attacked_tampered_negative):

        # anchor = self.embedding.data.repeat(attacked_positive.shape[0], 1).detach()
        feat_positive, feat_pos_anchor = self.feat_extract(attacked_positive)
        feat_negative, feat_neg_anchor = self.feat_extract(attacked_tampered_negative)

        pos_similarity = self.consine_similarity(feat_positive,feat_pos_anchor)
        neg_similarity = self.consine_similarity(feat_negative, feat_neg_anchor)

        loss = torch.sum(neg_similarity)-torch.sum(pos_similarity)

        return (loss, pos_similarity, neg_similarity), (feat_positive, feat_pos_anchor, feat_negative, feat_neg_anchor)

        # loss_triplet = self.triplet_loss(anchor, feat_positive, feat_negative)
        # # loss_l1 = self.l1_loss(feat_positive, anchor)
        # loss = 0.25*(loss_triplet)
        #
        # ### classification loss
        # label = torch.cat([torch.zeros((feat_negative.shape[0],1)), torch.ones((feat_positive.shape[0],1))],dim=0).cuda()
        # predicted_label = torch.cat([self.fc_classification(feat_negative),self.fc_classification(feat_positive)],dim=0)
        # loss_class = self.bce_loss(predicted_label, label)
        # loss += loss_class
        #
        # rate = 0.9
        # self.embedding.data.mul_(rate).add_(torch.mean(feat_positive,dim=0,keepdim=True), alpha=1 - rate)
        # return (loss, loss_triplet, loss_class), (anchor,feat_positive,feat_negative)



    def _make_layer(self, block, num_residual_blocks, intermediate_channels, stride, in_channel=None):
        if in_channel is None:
            in_channel = self.in_channels
        identity_downsample = None
        layers = []

        # Either if we half the input space for ex, 56x56 -> 28x28 (stride=2), or channels changes
        # we need to adapt the Identity (skip connection) so it will be able to be added
        # to the layer that's ahead
        if stride != 1 or in_channel != intermediate_channels * 4:
            identity_downsample = nn.Sequential(
                nn.Conv2d(
                    in_channel,
                    intermediate_channels * 4,
                    kernel_size=1,
                    stride=stride,
                    bias=False
                ),
                nn.BatchNorm2d(intermediate_channels * 4),
            )

        layers.append(
            block(in_channel, intermediate_channels, identity_downsample, stride)
        )

        # The expansion size is always 4 for ResNet 50,101,152
        self.in_channels = intermediate_channels * 4

        # For example for first resnet layer: 256 will be mapped to 64 as intermediate layer,
        # then finally back to 256. Hence no identity downsample is needed, since stride = 1,
        # and also same amount of channels.
        for i in range(num_residual_blocks - 1):
            layers.append(block(self.in_channels, intermediate_channels))

        return nn.Sequential(*layers)

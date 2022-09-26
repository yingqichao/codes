import copy
import logging
import os
import math
import cv2
import torch
import torch.distributed as dist
import torch.nn as nn
import torchvision
import torchvision.transforms.functional as F
from PIL import Image
from skimage.color import rgb2gray
from skimage.feature import canny
from torch.nn.parallel import DistributedDataParallel
from cycleisp_models.cycleisp import Raw2Rgb
import pytorch_ssim
from MVSS.models.mvssnet import get_mvss
from MVSS.models.resfcn import ResFCN
from metrics import PSNR
from models.modules.Quantization import diff_round
from noise_layers import *
from noise_layers.crop import Crop
from noise_layers.dropout import Dropout
from noise_layers.gaussian import Gaussian
from noise_layers.gaussian_blur import GaussianBlur
from noise_layers.middle_filter import MiddleBlur
from noise_layers.resize import Resize
from utils import stitch_images
from utils.JPEG import DiffJPEG
from .base_model import BaseModel
from data.pipeline import pipeline_tensor2image
# import matlab.engine
import torch.nn.functional as Functional
from utils.commons import create_folder
from data.pipeline import rawpy_tensor2image
# print("Starting MATLAB engine...")
# engine = matlab.engine.start_matlab()
# print("MATLAB engine loaded successful.")
# logger = logging.getLogger('base')
# json_path = '/qichaoying/Documents/COCOdataset/annotations/incnances_val2017.json'
# load coco data
# coco = COCO(annotation_file=json_path)
#
# # get all image index info
# ids = list(sorted(coco.imgs.keys()))
# print("number of images: {}".format(len(ids)))
#
# # get all coco class labels
# coco_classes = dict([(v["id"], v["name"]) for k, v in coco.cats.items()])
# import lpips
from MantraNet.mantranet import pre_trained_model
from .invertible_net import Inveritible_Decolorization_PAMI
from models.networks import UNetDiscriminator

class Modified_invISP(BaseModel):
    def __init__(self, opt, args, train_set=None):
        super(Modified_invISP, self).__init__(opt, args, train_set)
        self.train_set = train_set
        self.rank = torch.distributed.get_rank()
        self.opt = opt
        self.args = args
        self.train_opt = opt['train']
        self.test_opt = opt['test']
        self.real_H, self.real_H_path, self.previous_images, self.previous_previous_images = None, None, None, None
        self.previous_canny = None
        self.task_name = args.task_name #self.opt['datasets']['train']['name']  # self.train_opt['task_name']
        self.loading_from = args.loading_from
        self.is_load_models = args.load_models
        print("Task Name: {}".format(self.task_name))
        self.global_step = 0
        self.new_task = self.train_opt['new_task']
        self.use_gamma_correction = self.opt['use_gamma_correction']
        self.conduct_cropping = self.opt['conduct_cropping']
        self.consider_robost = self.opt['consider_robost']
        ####################################################################################################
        # todo: losses and attack layers
        # todo: JPEG attack rescaling deblurring
        ####################################################################################################
        self.tanh = nn.Tanh().cuda()
        self.psnr = PSNR(255.0).cuda()
        # self.lpips_vgg = lpips.LPIPS(net="vgg").cuda()
        # self.exclusion_loss = ExclusionLoss().type(torch.cuda.FloatTensor).cuda()
        self.ssim_loss = pytorch_ssim.SSIM().cuda()
        self.crop = Crop().cuda()
        self.dropout = Dropout().cuda()
        self.gaussian = Gaussian().cuda()
        self.salt_pepper = SaltPepper(prob=0.01).cuda()
        self.gaussian_blur = GaussianBlur().cuda()
        self.median_blur = MiddleBlur().cuda()
        self.resize = Resize().cuda()
        self.identity = Identity().cuda()
        self.jpeg_simulate = [
            [DiffJPEG(50, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(55, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(60, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(65, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(70, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(75, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(80, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(85, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(90, height=self.width_height, width=self.width_height).cuda(), ]
            , [DiffJPEG(95, height=self.width_height, width=self.width_height).cuda(), ]
        ]

        self.bce_loss = nn.BCELoss().cuda()
        self.bce_with_logit_loss = nn.BCEWithLogitsLoss().cuda()
        self.l1_loss = nn.SmoothL1Loss(beta=0.5).cuda()  # reduction="sum"
        self.l2_loss = nn.MSELoss().cuda()  # reduction="sum"
        # self.perceptual_loss = PerceptualLoss().cuda()
        # self.style_loss = StyleLoss().cuda()
        self.Quantization = diff_round
        # self.Quantization = Quantization().cuda()
        # self.Reconstruction_forw = ReconstructionLoss(losstype=self.train_opt['pixel_criterion_forw']).cuda()
        # self.Reconstruction_back = ReconstructionLoss(losstype=self.train_opt['pixel_criterion_back']).cuda()
        # self.criterion_adv = CWLoss().cuda()  # loss for fooling target model
        self.CE_loss = nn.CrossEntropyLoss().cuda()
        self.width_height = opt['datasets']['train']['GT_size']
        self.init_gaussian = None
        # self.adversarial_loss = AdversarialLoss(type="nsgan").cuda()

        ####################################################################################################
        # todo: TASKS Specification
        # todo: Why the networks are named like these? because their predecessors are named like these...
        # todo: in order to reduce modification, we let KD_JPEG=RAW2RAW network, generator=invISP, netG/discrimitator_mask/localizer=three detection networks
        ####################################################################################################
        self.network_list = []
        self.default_ISP_networks = ['generator']
        self.default_RAW_to_RAW_networks = ['KD_JPEG', 'qf_predict_network']
        self.default_detection_networks = ['netG', 'localizer', 'discriminator_mask', 'discriminator']
        if self.args.mode == 2:  # training the full isp protection pipeline
            ####################################################################################################
            # todo: TASKS: args.mode==2 training the full isp protection pipeline
            # todo:
            ####################################################################################################
            self.network_list = self.default_ISP_networks + self.default_RAW_to_RAW_networks + self.default_detection_networks
            print(f"network list:{self.network_list}")
        elif self.args.mode == 0:
            ####################################################################################################
            # todo: TASKS: args.mode==0 only traihing the detection networks
            # todo:
            ####################################################################################################
            self.network_list = self.default_ISP_networks + self.default_detection_networks
            print(f"network list:{self.network_list}")
        # elif self.args.mode==1:
        #     ####################################################################################################
        #     # todo: TASKS: args.mode==1 only traihing the invISP network and train identical function on RAW2RAW
        #     # todo:
        #     ####################################################################################################
        #     self.network_list = self.default_ISP_networks+self.default_RAW_to_RAW_networks
        #     print(f"network list:{self.network_list}")
        else:
            raise NotImplementedError('大神是不是搞错了？')

        ####################################################################################################
        # todo: Load models according to the specific mode
        # todo:
        ####################################################################################################
        if 'localizer' in self.network_list:
            ####################################################################################################
            # todo: Image Manipulation Detection Network (Downstream task) will be loaded
            # todo: mantranet: localizer mvssnet: netG resfcn: discriminator
            ####################################################################################################
            print("Building MantraNet...........please wait...")
            self.localizer = pre_trained_model(weight_path='./MantraNetv4.pt').cuda()
            self.localizer = DistributedDataParallel(self.localizer, device_ids=[torch.cuda.current_device()],
                                                     find_unused_parameters=True)

            print("Building MVSS...........please wait...")
            model_path = './MVSS/ckpt/mvssnet_casia.pt'
            self.netG = get_mvss(backbone='resnet50',
                                 pretrained_base=True,
                                 nclass=1,
                                 sobel=True,
                                 constrain=True,
                                 n_input=3,
                                 ).cuda()
            checkpoint = torch.load(model_path, map_location='cpu')
            self.netG.load_state_dict(checkpoint, strict=True)
            self.netG = DistributedDataParallel(self.netG, device_ids=[torch.cuda.current_device()],
                                                find_unused_parameters=True)
            print("Building ResFCN...........please wait...")
            self.discriminator_mask = ResFCN().cuda()
            self.discriminator_mask = DistributedDataParallel(self.discriminator_mask,
                                                              device_ids=[torch.cuda.current_device()],
                                                              find_unused_parameters=True)
            ## AS for ResFCN, we found no checkpoint in the official repo currently

            self.scaler_localizer = torch.cuda.amp.GradScaler()
            self.scaler_G = torch.cuda.amp.GradScaler()
            self.scaler_discriminator_mask = torch.cuda.amp.GradScaler()

        if 'generator' in self.network_list:
            ####################################################################################################
            # todo: ISP networks will be loaded
            # todo: invISP: generator
            ####################################################################################################
            from invISP_models.invISP_model import InvISPNet
            self.generator = Inveritible_Decolorization_PAMI(dims_in=[[3, 64, 64]], block_num=[2, 2, 2], augment=False,
                                                    ).cuda() #InvISPNet(channel_in=3, channel_out=3, block_num=4, network="ResNet").cuda()
            self.generator = DistributedDataParallel(self.generator, device_ids=[torch.cuda.current_device()],
                                                     find_unused_parameters=True)

            self.qf_predict_network = UNetDiscriminator(in_channels=3, out_channels=3,use_SRM=False).cuda()
            self.qf_predict_network = DistributedDataParallel(self.qf_predict_network,
                                                              device_ids=[torch.cuda.current_device()],
                                                              find_unused_parameters=True)

            from lama_models.HWMNet import HWMNet
            self.netG = HWMNet(in_chn=3, wf=32, depth=4).cuda()
            self.netG = DistributedDataParallel(self.netG, device_ids=[torch.cuda.current_device()],
                                                   find_unused_parameters=True)

            self.discriminator_mask = HWMNet(in_chn=3, out_chn=1, wf=32, depth=4).cuda() #UNetDiscriminator(use_SRM=False).cuda() #
            self.discriminator_mask = DistributedDataParallel(self.discriminator_mask,
                                                              device_ids=[torch.cuda.current_device()],
                                                              find_unused_parameters=True)

            self.discriminator = HWMNet(in_chn=3, out_chn=1, wf=32, depth=4).cuda() #UNetDiscriminator(use_SRM=False).cuda()
            self.discriminator = DistributedDataParallel(self.discriminator,
                                                              device_ids=[torch.cuda.current_device()],
                                                              find_unused_parameters=True)

            self.scaler_G = torch.cuda.amp.GradScaler()

            self.scaler_generator = torch.cuda.amp.GradScaler()
            self.scaler_qf = torch.cuda.amp.GradScaler()

        if 'KD_JPEG' in self.network_list:
            ####################################################################################################
            # todo: RAW2RAW network will be loaded
            # todo:
            ####################################################################################################
            from lama_models.HWMNet import HWMNet
            self.KD_JPEG = HWMNet(in_chn=1, wf=32, depth=4).cuda() # UNetDiscriminator(in_channels=1,use_SRM=False).cuda()
            self.KD_JPEG = DistributedDataParallel(self.KD_JPEG, device_ids=[torch.cuda.current_device()],
                                                   find_unused_parameters=True)

            self.scaler_kd_jpeg = torch.cuda.amp.GradScaler()

        ####################################################################################################
        # todo: Optimizers
        # todo: invISP
        ####################################################################################################
        wd_G = self.train_opt['weight_decay_G'] if self.train_opt['weight_decay_G'] else 0

        if 'netG' in self.network_list:
            self.optimizer_G = self.create_optimizer(self.netG,
                                                     lr=self.train_opt['lr_finetune'], weight_decay=wd_G)
        if 'discriminator_mask' in self.network_list:
            self.optimizer_discriminator_mask = self.create_optimizer(self.discriminator_mask,
                                                                      lr=self.train_opt['lr_scratch'], weight_decay=wd_G)
        if 'localizer' in self.network_list:
            self.optimizer_localizer = self.create_optimizer(self.localizer,
                                                             lr=self.train_opt['lr_scratch'], weight_decay=wd_G)
        if 'KD_JPEG' in self.network_list:
            self.optimizer_KD_JPEG = self.create_optimizer(self.KD_JPEG,
                                                           lr=self.train_opt['lr_scratch'], weight_decay=wd_G)
        # if 'discriminator' in self.network_list:
        #     self.optimizer_discriminator = self.create_optimizer(self.discriminator,
        #                                                          lr=self.train_opt['lr_scratch'], weight_decay=wd_G)
        if 'generator' in self.network_list:
            self.optimizer_generator = self.create_optimizer(self.generator,
                                                             lr=self.train_opt['lr_finetune'], weight_decay=wd_G)
        if 'qf_predict_network' in self.network_list:
            self.optimizer_qf = self.create_optimizer(self.qf_predict_network,
                                                      lr=self.train_opt['lr_scratch'], weight_decay=wd_G)

        ####################################################################################################
        # todo: Scheduler
        # todo: invISP
        ####################################################################################################
        self.schedulers = []
        for optimizer in self.optimizers:
            self.schedulers.append(torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=118287))

        ####################################################################################################
        # todo: init constants
        # todo: invISP
        ####################################################################################################
        self.forward_image_buff = None
        self.reloaded_time = 0
        self.basic_weight_fw = 5

        ####################################################################################################
        # todo: Loading Pretrained models
        # todo: note: these networks are supposed to be stored together. Can be further customized in the future
        ####################################################################################################
        # good_models: '/model/Rerun_4/29999'
        load_models = self.is_load_models>0
        load_state = False


        if self.args.mode == 0:
            self.out_space_storage = f"{self.opt['name']}/tamper_results"
            self.model_storage = f'/model/{self.loading_from}/'  # {self.task_name}_2

            self.load_space_storage = f"{self.opt['name']}/tamper_results"
            self.load_storage = f'/model/{self.loading_from}/'
            self.model_path = str(self.is_load_models)  # last: 18999

            print(f"loading models: {self.network_list}")
            if load_models:
                self.pretrain = self.load_space_storage + self.load_storage + self.model_path
                self.reload(self.pretrain, self.network_list)
        # elif self.args.mode==1:
        #     self.out_space_storage = f"{self.opt['name']}/ISP_results'
        #     self.model_storage = f'/model/{self.loading_from}/'
        #     self.model_path = str(26999) # 29999
        else:  # if self.args.mode==2:
            self.out_space_storage = f"{self.opt['name']}/complete_results"
            self.model_storage = f'/model/{self.loading_from}/'

            ### quick note: networks loading
            # self.network_list = self.default_ISP_networks + self.default_RAW_to_RAW_networks + self.default_detection_networks # mode==2
            # self.network_list = self.default_ISP_networks + self.default_detection_networks # mode==0

            self.load_space_storage = f"{self.opt['name']}/complete_results"
            self.load_storage = f'/model/{self.loading_from}/'
            self.model_path = str(self.is_load_models)  # last time: 10999

            print(f"loading tampering/ISP models: {self.network_list}")
            if load_models:
                self.pretrain = self.load_space_storage + self.load_storage + self.model_path
                self.reload(self.pretrain, network_list=self.default_ISP_networks + self.default_detection_networks)

            self.load_space_storage = f"{self.opt['name']}/complete_results"
            self.load_storage = f'/model/{self.loading_from}/'
            self.model_path = str(self.is_load_models)  # last time: 10999

            print(f"loading models: {self.network_list}")
            if load_models:
                self.pretrain = self.load_space_storage + self.load_storage + self.model_path
                self.reload(self.pretrain, network_list=self.default_RAW_to_RAW_networks)

        ####################################################################################################
        # todo: creating dirs
        # todo:
        ####################################################################################################
        create_folder(self.out_space_storage)
        create_folder(self.out_space_storage + "/model")
        create_folder(self.out_space_storage + "/images")
        create_folder(self.out_space_storage + "/isp_images/")
        create_folder(self.out_space_storage + "/model/" + self.task_name)
        create_folder(self.out_space_storage + "/images/" + self.task_name)
        create_folder(self.out_space_storage + "/isp_images/" + self.task_name)

        # ## load states
        # state_path = self.load_space_storage + self.load_storage + '{}.state'.format(self.model_path)
        # if load_state:
        #     print('Loading training state')
        #     if os.path.exists(state_path):
        #         self.resume_training(state_path, self.network_list)
        #     else:
        #         print('Did not find state [{:s}] ...'.format(state_path))

    def create_optimizer(self, net, lr=1e-4, weight_decay=0):
        ## lr should be train_opt['lr_scratch'] in default
        optim_params = []
        for k, v in net.named_parameters():
            if v.requires_grad:
                optim_params.append(v)
            # else:
            #     if self.rank <= 0:
            #         print('Params [{:s}] will not optimize.'.format(k))
        optimizer = torch.optim.AdamW(optim_params, lr=lr,
                                      weight_decay=weight_decay,
                                      betas=(0.9, 0.99))  # train_opt['beta1'], train_opt['beta2']
        self.optimizers.append(optimizer)

        return optimizer

    def clamp_with_grad(self, tensor):
        tensor_clamp = torch.clamp(tensor, 0, 1)
        return tensor + (tensor_clamp - tensor).clone().detach()

    def gaussian_batch(self, dims):
        return self.clamp_with_grad(torch.randn(tuple(dims)).cuda())

    def feed_data_router(self, batch, mode):
        if mode == 0.0:
            self.feed_data_ISP(batch, mode='train') # feed_data_COCO_like(batch)
        else:
            self.feed_data_ISP(batch, mode='train')

    def feed_data_val_router(self, batch, mode):
        if mode == 0.0:
            self.feed_data_ISP(batch, mode='val')  # feed_data_COCO_like(batch)
        else:
            self.feed_data_ISP(batch, mode='val')

    def feed_data_ISP(self, batch, mode='train'):
        if mode=='train':
            self.real_H = batch['input_raw'].cuda()
            self.label = batch['target_rgb'].cuda()
            self.file_name = batch['file_name']
            self.camera_white_balance = batch['camera_whitebalance'].cuda()
            self.bayer_pattern = batch['bayer_pattern'].cuda()
            self.camera_name = batch['camera_name']
        else:
            self.real_H_val = batch['input_raw'].cuda()
            self.label_val = batch['target_rgb'].cuda()
            self.file_name_val = batch['file_name']
            self.camera_white_balance_val = batch['camera_whitebalance'].cuda()
            self.bayer_pattern_val = batch['bayer_pattern'].cuda()
            self.camera_name_val = batch['camera_name']

    def feed_data_COCO_like(self, batch):
        img, label, canny_image = batch
        self.real_H = img.cuda()
        self.canny_image = canny_image.cuda()

    def optimize_parameters_router(self, mode, step=None):
        if mode == 0.0:
            return self.optimize_parameters_prepare(step=step)
        # elif mode==1.0:
        #     return self.optimize_parameters_main()
        else:
            return self.optimize_parameters_main(step=step)

    def gamma_correction(self, tensor, avg=4095, digit=2.2):
    ## gamma correction
    #             norm_value = np.power(4095, 1 / 2.2) if self.camera_name == 'Canon_EOS_5D' else np.power(16383, 1 / 2.2)
    #             input_raw_img = np.power(input_raw_img, 1 / 2.2)
        norm = math.pow(avg, 1 / digit)
        tensor = torch.pow(tensor*avg, 1/digit)
        tensor = tensor / norm

        return tensor

    @torch.no_grad()
    def _momentum_update_key_encoder(self, momentum=0.9):
        ####################################################################################################
        # todo:  Momentum update of the key encoder
        # todo: param_k: momentum
        ####################################################################################################

        for param_q, param_k in zip(self.discriminator_mask.parameters(), self.discriminator.parameters()):
            param_k.data = param_k.data * momentum + param_q.data * (1. - momentum)


    def optimize_parameters_main(self, step=None):
        ####################################################################################################
        # todo: Image Manipulation Detection Network (Downstream task)
        # todo: mantranet: localizer mvssnet: netG resfcn: discriminator
        ####################################################################################################
        did_val = False

        if step is not None:
            self.global_step = step

        train_isp_networks = self.opt["train_isp_networks"]
        train_full_pipeline = self.opt["train_full_pipeline"]

        logs, debug_logs = {}, []
        average_PSNR = 50  # default
        self.real_H = self.clamp_with_grad(self.real_H)
        lr = self.get_current_learning_rate()
        logs['lr'] = lr
        step_acumulate = self.opt["step_acumulate"]
        stored_image_netG = None
        stored_image_generator = None
        stored_image_qf_predict = None


        if not (self.previous_images is None or self.previous_previous_images is None):
            sum_batch_size = self.real_H.shape[0]
            num_per_clip = int(sum_batch_size//step_acumulate)

            for idx_clip in range(step_acumulate):

                with torch.enable_grad() if train_isp_networks else torch.no_grad():
                    if train_isp_networks:
                        self.generator.train()
                        self.netG.train()
                        self.qf_predict_network.train()
                    else:
                        self.generator.eval()
                        self.netG.eval()
                        self.qf_predict_network.eval()
                    ####################################################################################################
                    # todo: Image pipeline training
                    # todo: we first train several nn-based ISP networks
                    ####################################################################################################
                    ### tensor sized (B,3)
                    camera_white_balance = self.camera_white_balance[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                    file_name = self.file_name[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip]
                    ### tensor sized (B,1) ranging from [0,3]
                    bayer_pattern = self.bayer_pattern[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()

                    input_raw_one_dim = self.real_H[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                    gt_rgb = self.label[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                    input_raw = self.visualize_raw(input_raw_one_dim,
                                                   bayer_pattern=bayer_pattern, white_balance=camera_white_balance, eval=not train_isp_networks)
                    batch_size, num_channels, height_width, _ = input_raw.shape
                    # input_raw = self.clamp_with_grad(input_raw)

                    ####### CYCYLE ISP PRODUCES WRONG RESULTS, ABANDON! 20220926 ##############
                    # modified_input_qf_predict = self.qf_predict_network(rgb=gt_rgb, raw=input_raw.clone().detach())
                    # if self.use_gamma_correction:
                    #     modified_input_qf_predict = self.gamma_correction(modified_input_qf_predict)
                    #
                    # CYCLE_L1 = self.l1_loss(input=modified_input_qf_predict, target=gt_rgb)
                    # CYCLE_SSIM = - self.ssim_loss(modified_input_qf_predict, gt_rgb)
                    # CYCLE_loss = CYCLE_L1 + 0.01* CYCLE_SSIM
                    # modified_input_qf_predict_detach = self.clamp_with_grad(modified_input_qf_predict.detach())
                    # CYCLE_PSNR = self.psnr(self.postprocess(modified_input_qf_predict_detach), self.postprocess(gt_rgb)).item()
                    # logs['CYCLE_PSNR'] = CYCLE_PSNR
                    # # del modified_input_qf_predict
                    # # torch.cuda.empty_cache()
                    # stored_image_qf_predict = modified_input_qf_predict_detach if stored_image_qf_predict is None else \
                    #     torch.cat((stored_image_generator,modified_input_qf_predict_detach),dim=0)
                    #
                    # if train_isp_networks:
                    #     # self.optimizer_generator.zero_grad()
                    #     CYCLE_loss.backward()
                    #     # self.scaler_qf.scale(CYCLE_loss).backward()
                    #     if idx_clip % step_acumulate == step_acumulate - 1:
                    #         if self.train_opt['gradient_clipping']:
                    #             nn.utils.clip_grad_norm_(self.qf_predict_network.parameters(), 1)
                    #             # nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
                    #         self.optimizer_qf.step()
                    #         # self.scaler_qf.step(self.optimizer_qf)
                    #         # self.scaler_qf.update()
                    #         self.optimizer_qf.zero_grad()

                    modified_input_netG = self.netG(input_raw.clone().detach())
                    if self.use_gamma_correction:
                        modified_input_netG = self.gamma_correction(modified_input_netG)

                    THIRD_L1 = self.l1_loss(input=modified_input_netG, target=gt_rgb)
                    THIRD_SSIM = - self.ssim_loss(modified_input_netG, gt_rgb)
                    THIRD_loss = THIRD_L1 + 0.01 * THIRD_SSIM
                    modified_input_netG_detach = self.clamp_with_grad(modified_input_netG.detach())
                    PIPE_PSNR = self.psnr(self.postprocess(modified_input_netG_detach),self.postprocess(gt_rgb)).item()
                    logs['PIPE_PSNR'] = PIPE_PSNR
                    # del modified_input_netG
                    # torch.cuda.empty_cache()
                    stored_image_netG = modified_input_netG_detach if stored_image_netG is None else \
                        torch.cat((stored_image_netG, modified_input_netG_detach), dim=0)

                    if train_isp_networks:
                        # self.optimizer_generator.zero_grad()
                        THIRD_loss.backward()
                        # self.scaler_G.scale(THIRD_loss).backward()
                        if idx_clip % step_acumulate == step_acumulate - 1:
                            if self.train_opt['gradient_clipping']:
                                nn.utils.clip_grad_norm_(self.netG.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
                            self.optimizer_G.step()
                            # self.scaler_G.step(self.optimizer_G)
                            # self.scaler_G.update()
                            self.optimizer_G.zero_grad()

                    modified_input_generator = self.generator(input_raw.clone().detach())
                    if self.use_gamma_correction:
                        modified_input_generator = self.gamma_correction(modified_input_generator)

                    ISP_L1_FOR = self.l1_loss(input=modified_input_generator, target=gt_rgb)
                    ISP_SSIM = - self.ssim_loss(modified_input_generator, gt_rgb)
                    ISP_loss = ISP_L1_FOR + 0.01 * ISP_SSIM
                    modified_input_generator = self.clamp_with_grad(modified_input_generator)
                    input_raw_rev, _ = self.generator(modified_input_generator, rev=True)
                    ISP_L1_REV = self.l1_loss(input=input_raw_rev, target=input_raw.clone().detach())
                    ISP_L1 = 0
                    ISP_L1 += ISP_L1_FOR
                    ISP_L1 += ISP_L1_REV
                    modified_input_generator_detach = modified_input_generator.detach()
                    ISP_PSNR = self.psnr(self.postprocess(modified_input_generator_detach), self.postprocess(gt_rgb)).item()
                    logs['ISP_PSNR'] = ISP_PSNR
                    # del modified_input_generator
                    # del input_raw_rev
                    # torch.cuda.empty_cache()
                    stored_image_generator = modified_input_generator_detach if stored_image_generator is None else \
                        torch.cat((stored_image_generator, modified_input_generator_detach), dim=0)

                    if train_isp_networks:
                        ####################################################################################################
                        # todo: Grad Accumulation
                        # todo: added 20220919, steo==0, do not update, step==1 update
                        ####################################################################################################
                        # self.optimizer_generator.zero_grad()
                        ISP_loss.backward()
                        # self.scaler_generator.scale(ISP_loss).backward()
                        if idx_clip % step_acumulate==step_acumulate-1:
                            if self.train_opt['gradient_clipping']:
                                nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
                            self.optimizer_generator.step()
                            # self.scaler_generator.step(self.optimizer_generator)
                            # self.scaler_generator.update()
                            self.optimizer_generator.zero_grad()

                ####################################################################################################
                # todo: emptying cache to save memory
                # todo: https://discuss.pytorch.org/t/how-to-delete-a-tensor-in-gpu-to-free-up-memory/48879/25
                ####################################################################################################
                # else:
                #     with torch.no_grad():
                #         self.qf_predict_network.eval()
                #         self.generator.eval()
                #         self.netG.eval()
                #         modified_input_qf_predict = self.qf_predict_network(rgb=gt_rgb, raw=input_raw)
                #         modified_input_qf_predict = self.clamp_with_grad(modified_input_qf_predict.detach())
                #         CYCLE_PSNR = self.psnr(self.postprocess(modified_input_qf_predict), self.postprocess(gt_rgb)).item()
                #         logs['CYCLE_PSNR'] = CYCLE_PSNR
                #
                #         modified_input_generator = self.generator(input_raw)
                #         modified_input_generator = self.clamp_with_grad(modified_input_generator.detach())
                #         ISP_PSNR = self.psnr(self.postprocess(modified_input_generator),
                #                              self.postprocess(gt_rgb)).item()
                #         logs['ISP_PSNR'] = ISP_PSNR
                #
                #         modified_input_netG = self.netG(input_raw)
                #         modified_input_netG = self.clamp_with_grad(modified_input_netG.detach())
                #         PIPE_PSNR = self.psnr(self.postprocess(modified_input_netG),
                #                              self.postprocess(gt_rgb)).item()
                #         logs['PIPE_PSNR'] = PIPE_PSNR

                # ####################################################################################################
                # # todo: Image pipeline using my_own_pipeline
                # # todo: the result is not so good
                # ####################################################################################################
                # modified_input_netG = torch.zeros_like(gt_rgb)
                # for img_idx in range(batch_size):
                #     numpy_rgb = input_raw[img_idx].clone().detach().permute(1, 2, 0).contiguous()
                #     file_name = self.file_name[img_idx]
                #     metadata = self.train_set.metadata_list[file_name]
                #     numpy_rgb = pipeline_tensor2image(raw_image=numpy_rgb,
                #                                       metadata=metadata['metadata'],
                #                                       input_stage='demosaic')
                #
                #     modified_input_netG[img_idx] = torch.from_numpy(
                #         np.ascontiguousarray(np.transpose(numpy_rgb, (2, 0, 1)))).contiguous().float()
                #
                # ISP_PSNR_PIPE = self.psnr(self.postprocess(modified_input_netG), self.postprocess(gt_rgb)).item()
                # logs['PIPE_PSNR'] = ISP_PSNR_PIPE
                if not train_full_pipeline and (self.global_step % 200 == 3 or self.global_step <= 10):
                    images = stitch_images(
                        self.postprocess(input_raw),
                        self.postprocess(modified_input_generator_detach),
                        # self.postprocess(modified_input_qf_predict_detach),
                        self.postprocess(modified_input_netG_detach),
                        self.postprocess(gt_rgb),

                        img_per_row=1
                    )

                    name = f"{self.out_space_storage}/isp_images/{self.task_name}/{str(self.global_step).zfill(5)}" \
                           f"_{idx_clip}_ {str(self.rank)}.png"
                    print(f'Bayer: {bayer_pattern}. Saving sample {name}')
                    images.save(name)

            if train_full_pipeline:
                self.KD_JPEG.train()
                self.generator.eval()
                self.netG.eval()
                self.discriminator_mask.train()
                self.discriminator.eval()
                self.qf_predict_network.train()

                for idx_clip in range(step_acumulate):
                    with torch.enable_grad():

                        ####################################################################################################
                        # todo: Generation of protected RAW
                        # todo: next, we protect RAW for tampering detection
                        ####################################################################################################
                        input_raw_one_dim = self.real_H[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                        file_name = self.file_name[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip]
                        camera_name = self.camera_name[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip]
                        gt_rgb = self.label[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                        ### tensor sized (B,3)
                        camera_white_balance = self.camera_white_balance[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                        ### tensor sized (B,1) ranging from [0,3]
                        bayer_pattern = self.bayer_pattern[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()

                        input_raw = self.visualize_raw(input_raw_one_dim, bayer_pattern=bayer_pattern, white_balance=camera_white_balance)
                        batch_size, num_channels, height_width, _ = input_raw.shape


                        modified_raw_one_dim = self.KD_JPEG(input_raw_one_dim)
                        RAW_L1 = self.l1_loss(input=modified_raw_one_dim, target=input_raw_one_dim)
                        modified_raw_one_dim = self.clamp_with_grad(modified_raw_one_dim)

                        ####################################################################################################
                        # todo: doing raw visualizing and white balance (and gamma)
                        # todo:
                        ####################################################################################################

                        modified_raw = self.visualize_raw(modified_raw_one_dim, bayer_pattern=bayer_pattern, white_balance=camera_white_balance)
                        RAW_PSNR = self.psnr(self.postprocess(modified_raw), self.postprocess(input_raw)).item()
                        logs['RAW_PSNR'] = RAW_PSNR

                        ####################################################################################################
                        # todo: RAW2RGB pipelines
                        # todo: note: our goal is that the rendered rgb by the protected RAW should be close to that rendered by unprotected RAW
                        # todo: thus, we are not let the ISP network approaching the ground-truth RGB.
                        ####################################################################################################

                        #### DUE TO THE MISCONDUCTIVENESS OF CYCLEISP, WE RETRAIN A MODEL #########
                        modified_input_qf_predict = self.qf_predict_network(input_raw.clone().detach())
                        if self.use_gamma_correction:
                            modified_input_qf_predict = self.gamma_correction(modified_input_qf_predict)

                        CYCLE_L1 = self.l1_loss(input=modified_input_qf_predict, target=gt_rgb)
                        CYCLE_SSIM = - self.ssim_loss(modified_input_qf_predict, gt_rgb)
                        CYCLE_loss = CYCLE_L1 + 0.01* CYCLE_SSIM
                        modified_input_qf_predict_detach = self.clamp_with_grad(modified_input_qf_predict.detach())
                        CYCLE_PSNR = self.psnr(self.postprocess(modified_input_qf_predict_detach), self.postprocess(gt_rgb)).item()
                        logs['CYCLE_PSNR'] = CYCLE_PSNR
                        logs['CYCLE_L1'] = CYCLE_L1.item()
                        # del modified_input_qf_predict
                        # torch.cuda.empty_cache()
                        stored_image_qf_predict = modified_input_qf_predict_detach if stored_image_qf_predict is None else \
                            torch.cat((stored_image_generator,modified_input_qf_predict_detach),dim=0)

                        # self.optimizer_generator.zero_grad()
                        CYCLE_loss.backward()
                        # self.scaler_qf.scale(CYCLE_loss).backward()
                        if idx_clip % step_acumulate == step_acumulate - 1:
                            if self.train_opt['gradient_clipping']:
                                nn.utils.clip_grad_norm_(self.qf_predict_network.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
                            self.optimizer_qf.step()
                            # self.scaler_qf.step(self.optimizer_qf)
                            # self.scaler_qf.update()
                            self.optimizer_qf.zero_grad()


                        ######## we use invISP ########
                        modified_input_0 = self.generator(modified_raw)
                        if self.use_gamma_correction:
                            modified_input_0 = self.gamma_correction(modified_input_0)
                        tamper_source_0 = stored_image_generator[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                        ISP_L1_0 = self.l1_loss(input=modified_input_0, target=tamper_source_0)
                        ISP_SSIM_0 = - self.ssim_loss(modified_input_0, tamper_source_0)
                        modified_input_0 = self.clamp_with_grad(modified_input_0)

                        ######## we use cycleISP ########
                        modified_input_1 = self.qf_predict_network(modified_raw)
                        if self.use_gamma_correction:
                            modified_input_1 = self.gamma_correction(modified_input_1)
                        tamper_source_1 = stored_image_qf_predict[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                        ISP_L1_1 = self.l1_loss(input=modified_input_1, target=tamper_source_1)
                        ISP_SSIM_1 = - self.ssim_loss(modified_input_1, tamper_source_1)
                        modified_input_1 = self.clamp_with_grad(modified_input_1)
                        ######## we use my_own_pipeline ########
                        modified_input_2 = self.netG(modified_raw)
                        if self.use_gamma_correction:
                            modified_input_2 = self.gamma_correction(modified_input_2)
                        tamper_source_2 = stored_image_netG[idx_clip * num_per_clip:(idx_clip + 1) * num_per_clip].contiguous()
                        ISP_L1_2 = self.l1_loss(input=modified_input_1, target=tamper_source_2)
                        ISP_SSIM_2 = - self.ssim_loss(modified_input_1, tamper_source_2)
                        modified_input_2 = self.clamp_with_grad(modified_input_2)
                        # modified_pack_raw = self.pack_raw(modified_raw_one_dim)
                        # modified_input_2 = torch.zeros_like(gt_rgb)
                        # for img_idx in range(batch_size):
                        #     numpy_rgb = modified_pack_raw[img_idx].clone().detach().permute(1, 2, 0).contiguous()
                        #     file_name = self.file_name[img_idx]
                        #     metadata = self.train_set.metadata_list[file_name]
                        #     numpy_rgb = pipeline_tensor2image(raw_image=numpy_rgb,
                        #                                       metadata=metadata['metadata'],
                        #                                       input_stage='demosaic')
                        #
                        #     modified_input_2[img_idx] = torch.from_numpy(np.ascontiguousarray(np.transpose(numpy_rgb, (2, 0, 1)))).contiguous().float()
                        #
                        # modified_input_2 = modified_raw + (modified_input_2-modified_raw).detach()
                        # tamper_source_2 = modified_input_netG

                        ####################################################################################################
                        # todo: doing mixup on the images
                        # todo: note: our goal is that the rendered rgb by the protected RAW should be close to that rendered by unprotected RAW
                        # todo: thus, we are not let the ISP network approaching the ground-truth RGB.
                        ####################################################################################################
                        alpha_0 = np.random.rand()*0.66
                        alpha_1 = np.random.rand()*0.66
                        alpha_1 = min(alpha_1,1-alpha_0)
                        alpha_1 = max(0, alpha_1)
                        # alpha_2 = np.random.rand() * 0.6
                        # alpha_2 = min(alpha_2, 1 - alpha_0-alpha_1)
                        # alpha_2 = max(0, alpha_2)
                        alpha_2 = 1 - alpha_0-alpha_1

                        modified_input = alpha_0*modified_input_0+alpha_1*modified_input_1+alpha_2*modified_input_2
                        tamper_source = (alpha_0*tamper_source_0+alpha_1*tamper_source_1+alpha_2*tamper_source_2).detach()

                        modified_input = self.clamp_with_grad(modified_input)
                        tamper_source = self.clamp_with_grad(tamper_source)

                        ISP_PSNR = self.psnr(self.postprocess(modified_input), self.postprocess(tamper_source)).item()
                        logs['ISP_PSNR_NOW'] = ISP_PSNR

                        ####################################################################################################
                        # todo: Image tampering and benign attacks
                        # todo:
                        ####################################################################################################

                        ####################################################################################################
                        # todo: cropping
                        # todo: cropped: original-sized cropped image, scaled_cropped: resized cropped image, masks, masks_GT
                        ####################################################################################################

                        if self.conduct_cropping:
                            locs, cropped, scaled_cropped = self.cropping_mask_generation(
                                forward_image=modified_input,  min_rate=0.7, max_rate=1.0, logs=logs)
                            h_start, h_end, w_start, w_end = locs

                            _, _, tamper_source_cropped = self.cropping_mask_generation(forward_image=tamper_source, locs=locs, logs=logs)
                        else:
                            scaled_cropped = modified_input
                            tamper_source_cropped = tamper_source


                        percent_range = (0.05, 0.30)
                        masks, masks_GT = self.mask_generation(modified_input=modified_input, percent_range=percent_range, logs=logs)

                        # attacked_forward = tamper_source_cropped
                        attacked_forward, masks, masks_GT = self.tampering(
                            forward_image=tamper_source_cropped, masks=masks, masks_GT=masks_GT,
                            modified_input=scaled_cropped, percent_range=percent_range, logs=logs)

                        if self.consider_robost:
                            if self.using_weak_jpeg_plus_blurring_etc():
                                quality_idx = np.random.randint(19, 21)
                            else:
                                quality_idx = np.random.randint(10, 19)
                            attacked_image = self.benign_attacks(attacked_forward=attacked_forward, logs=logs,
                                                                 quality_idx=quality_idx)
                        else:
                            attacked_image = attacked_forward

                        # ERROR = attacked_image-attacked_forward
                        error_l1 = self.psnr(self.postprocess(attacked_image), self.postprocess(attacked_forward)).item() #self.l1_loss(input=ERROR, target=torch.zeros_like(ERROR))
                        logs['ERROR'] = error_l1
                        ####################################################################################################
                        # todo: Image Manipulation Detection Network (Downstream task)
                        # todo: mantranet: localizer mvssnet: netG resfcn: discriminator
                        ####################################################################################################
                        # _, pred_mvss = self.netG(attacked_image)
                        # CE_MVSS = self.bce_with_logit_loss(pred_mvss, masks_GT)
                        #
                        # pred_mantra = self.localizer(attacked_image)
                        # CE_mantra = self.bce_with_logit_loss(pred_mantra, masks_GT)
                        ####################################################################################################
                        # todo: update Image Manipulation Detection Network and re-calculate
                        # todo: why contiguous? https://discuss.pytorch.org/t/runtimeerror-set-sizes-and-strides-is-not-allowed-on-a-tensor-created-from-data-or-detach/116910/10
                        # todo: This seems to be related to the CUDNN implementation of Resize with conv2d,
                        # todo: specifically if your generator has any uses for channels being the last dimension. (eg. transformers) .

                        ####################################################################################################
                        pred_resfcn = self.discriminator_mask(attacked_image.detach().contiguous())
                        CE_resfcn = self.bce_with_logit_loss(pred_resfcn, masks_GT)

                        CE_resfcn.backward()
                        if idx_clip % step_acumulate == step_acumulate-1:
                            # self.optimizer_generator.zero_grad()
                            # loss.backward()
                            # self.scaler_generator.scale(loss).backward()
                            if self.train_opt['gradient_clipping']:
                                nn.utils.clip_grad_norm_(self.discriminator_mask.parameters(), 1)
                            self.optimizer_discriminator_mask.step()
                            self.optimizer_discriminator_mask.zero_grad()

                        ####################################################################################################
                        # todo: doing ema average
                        # todo:
                        ####################################################################################################

                        # logs['CE_MVSS'] = CE_MVSS.item()
                        # logs['CE_mantra'] = CE_mantra.item()

                        detection_model = self.discriminator if self.begin_using_momentum else self.discriminator_mask
                        pred_resfcn = detection_model(attacked_image.contiguous())
                        CE_resfcn = self.bce_with_logit_loss(pred_resfcn, masks_GT)
                        logs['CE_resfcn'] = CE_resfcn.item()

                        loss = 0
                        loss += (ISP_L1_0+ISP_L1_1+ISP_L1_2)/3 + 1 * RAW_L1
                        loss += 0.01 * (ISP_SSIM_0+ISP_SSIM_1+ISP_SSIM_2)/3
                        loss += 0.4 * CE_resfcn  # (CE_MVSS+CE_mantra+CE_resfcn)/3
                        logs['loss'] = loss.item()

                        ####################################################################################################
                        # todo: Grad Accumulation
                        # todo: added 20220919, steo==0, do not update, step==1 update
                        ####################################################################################################
                        loss.backward()
                        # self.scaler_kd_jpeg.scale(loss).backward()
                        if idx_clip % step_acumulate == step_acumulate-1:
                            # self.optimizer_generator.zero_grad()
                            # loss.backward()
                            # self.scaler_generator.scale(loss).backward()
                            if self.train_opt['gradient_clipping']:
                                nn.utils.clip_grad_norm_(self.KD_JPEG.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.netG.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.localizer.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.discriminator_mask.parameters(), 1)
                                # nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
                            self.optimizer_KD_JPEG.step()
                            # self.optimizer_discriminator_mask.step()
                            # self.scaler_kd_jpeg.step(self.optimizer_KD_JPEG)
                            # self.scaler_kd_jpeg.step(self.optimizer_G)
                            # self.scaler_kd_jpeg.step(self.optimizer_localizer)
                            # self.scaler_kd_jpeg.step(self.optimizer_discriminator_mask)
                            # self.scaler_kd_jpeg.update()

                            self.optimizer_KD_JPEG.zero_grad()
                            self.optimizer_G.zero_grad()
                            self.optimizer_localizer.zero_grad()
                            self.optimizer_discriminator_mask.zero_grad()
                            self.optimizer_G.zero_grad()


                    self._momentum_update_key_encoder()

                        # self.optimizer_G.zero_grad()
                        # # loss.backward()
                        # self.scaler_G.scale(CE_MVSS).backward(retain_graph=True)
                        # if self.train_opt['gradient_clipping']:
                        #     nn.utils.clip_grad_norm_(self.netG.parameters(), 1)
                        # # self.optimizer_G.step()
                        # self.scaler_G.step(self.optimizer_G)
                        # self.scaler_G.update()
                        #
                        # self.optimizer_localizer.zero_grad()
                        # # CE_train.backward()
                        # self.scaler_localizer.scale(CE_mantra).backward(retain_graph=True)
                        # if self.train_opt['gradient_clipping']:
                        #     nn.utils.clip_grad_norm_(self.localizer.parameters(), 1)
                        # # self.optimizer_localizer.step()
                        # self.scaler_localizer.step(self.optimizer_localizer)
                        # self.scaler_localizer.update()
                        #
                        # self.optimizer_discriminator_mask.zero_grad()
                        # # dis_loss.backward()
                        # self.scaler_discriminator_mask.scale(CE_resfcn).backward(retain_graph=True)
                        # if self.train_opt['gradient_clipping']:
                        #     nn.utils.clip_grad_norm_(self.discriminator_mask.parameters(), 1)
                        # # self.optimizer_discriminator_mask.step()
                        # self.scaler_discriminator_mask.step(self.optimizer_discriminator_mask)
                        # self.scaler_discriminator_mask.update()

                    ####################################################################################################
                    # todo: printing the images
                    # todo: invISP
                    ####################################################################################################
                    anomalies = False  # CE_recall.item()>0.5
                    if anomalies or self.global_step % 200 == 3 or self.global_step <= 10:
                        images = stitch_images(
                            self.postprocess(input_raw),
                            self.postprocess(modified_input_0),
                            self.postprocess(modified_input_1),
                            self.postprocess(modified_input_2),
                            self.postprocess(gt_rgb),
                            ### RAW2RAW
                            self.postprocess(modified_raw),
                            self.postprocess(10 * torch.abs(modified_raw - input_raw)),
                            ### RAW2RGB
                            self.postprocess(modified_input),
                            self.postprocess(tamper_source),
                            self.postprocess(10 * torch.abs(modified_input - tamper_source)),
                            ### tampering and benign attack
                            self.postprocess(attacked_forward),
                            self.postprocess(attacked_image),
                            self.postprocess(10 * torch.abs(attacked_forward - attacked_image)),
                            ### tampering detection
                            self.postprocess(masks_GT),
                            # self.postprocess(torch.sigmoid(pred_mvss)),
                            # self.postprocess(10 * torch.abs(masks_GT - torch.sigmoid(pred_mvss))),
                            # self.postprocess(torch.sigmoid(pred_mantra)),
                            # self.postprocess(10 * torch.abs(masks_GT - torch.sigmoid(pred_mantra))),
                            self.postprocess(torch.sigmoid(pred_resfcn)),
                            # self.postprocess(10 * torch.abs(masks_GT - torch.sigmoid(pred_resfcn))),
                            img_per_row=1
                        )

                        name = f"{self.out_space_storage}/images/{self.task_name}/{str(self.global_step).zfill(5)}" \
                                   f"_{idx_clip}_ {str(self.rank)}.png"
                        print('\nsaving sample ' + name)
                        images.save(name)

                ####################################################################################################
                # todo: inference single image for testing
                # todo:
                ####################################################################################################
                if self.global_step % 199 == 3 or self.global_step <= 10:
                    did_val = True
                    self.inference_single_image()#input_raw_one_dim=input_raw_one_dim, input_raw=input_raw, gt_rgb=gt_rgb,
                                                # camera_white_balance=camera_white_balance, file_name=file_name,
                                                # camera_name=camera_name, bayer_pattern=bayer_pattern)

        ####################################################################################################
        # todo: updating the training stage
        # todo:
        ####################################################################################################
        ######## Finally ####################
        if self.global_step % 1000 == 999 or self.global_step == 9:
            if self.rank == 0:
                print('Saving models and training states.')
                self.save(self.global_step, folder='model', network_list=self.network_list)
        if self.real_H is not None:
            if self.previous_images is not None:
                self.previous_previous_images = self.previous_images.clone().detach()
            self.previous_images = self.label
        self.global_step = self.global_step + 1

        # print(logs)
        # print(debug_logs)
        return logs, debug_logs, did_val

    def raw_py_test(self):
        pass

    @torch.no_grad()
    def inference_single_image(self):#,*, input_raw_one_dim, file_name, input_raw, gt_rgb, bayer_pattern,camera_name, camera_white_balance):
        ####################################################################################################
        # todo: inference single image
        # todo: what is tamper_source? used for simulated inpainting, only activated if self.global_step%3==2
        ####################################################################################################
        input_raw_one_dim = self.real_H_val
        file_name = self.file_name_val
        camera_name = self.camera_name_val
        gt_rgb = self.label_val
        ### tensor sized (B,3)
        camera_white_balance = self.camera_white_balance_val
        ### tensor sized (B,1) ranging from [0,3]
        bayer_pattern = self.bayer_pattern_val

        input_raw = self.visualize_raw(input_raw_one_dim, bayer_pattern=bayer_pattern,
                                       white_balance=camera_white_balance)
        batch_size, num_channels, height_width, _ = input_raw.shape
        
        batch_size = input_raw.shape[0]
        logs=[]

        self.KD_JPEG.eval()
        modified_raw_one_dim = self.KD_JPEG(input_raw_one_dim)
        modified_raw_one_dim = self.clamp_with_grad(modified_raw_one_dim)
        RAW_PSNR = self.psnr(self.postprocess(modified_raw_one_dim), self.postprocess(input_raw_one_dim)).item()

        modified_raw = self.visualize_raw(modified_raw_one_dim,
                                          bayer_pattern=bayer_pattern, white_balance=camera_white_balance)

        if self.using_invISP():
            self.generator.eval()
            ######## we use invISP ########
            modified_input = self.generator(modified_raw)
            if self.use_gamma_correction:
                modified_input = self.gamma_correction(modified_input)
            tamper_source = self.generator(input_raw)
        elif self.using_cycleISP():
            self.qf_predict_network.eval()
            modified_input = self.qf_predict_network(modified_raw)
            if self.use_gamma_correction:
                modified_input = self.gamma_correction(modified_input)
            tamper_source = self.qf_predict_network(input_raw)
        elif self.using_my_own_pipeline():
            self.netG.eval()
            ######## we use invISP ########
            modified_input = self.netG(modified_raw)
            if self.use_gamma_correction:
                modified_input = self.gamma_correction(modified_input)
            tamper_source = self.netG(input_raw)
            # modified_input = torch.zeros_like(gt_rgb)
            # for img_idx in range(batch_size):
            #     numpy_rgb = modified_pack_raw[img_idx].clone().detach().permute(1, 2, 0).contiguous()
            #     file_name = self.file_name[img_idx]
            #     metadata = self.train_set.metadata_list[file_name]
            #     numpy_rgb = pipeline_tensor2image(raw_image=numpy_rgb,
            #                                       metadata=metadata['metadata'],
            #                                       input_stage='demosaic')
            #
            #     modified_input[img_idx] = torch.from_numpy(
            #         np.ascontiguousarray(np.transpose(numpy_rgb, (2, 0, 1)))).contiguous().float()
            #
            # tamper_source = torch.zeros_like(gt_rgb)
            # for img_idx in range(batch_size):
            #     numpy_rgb = modified_raw[img_idx].clone().detach().permute(1, 2, 0).contiguous()
            #     file_name = self.file_name[img_idx]
            #     metadata = self.train_set.metadata_list[file_name]
            #     numpy_rgb = pipeline_tensor2image(raw_image=numpy_rgb,
            #                                       metadata=metadata['metadata'],
            #                                       input_stage='demosaic')
            #
            #     tamper_source[img_idx] = torch.from_numpy(
            #         np.ascontiguousarray(np.transpose(numpy_rgb, (2, 0, 1)))).contiguous().float()
        else:
            ####################################################################################################
            # todo: rawpy transforming RAW tensor into RGB numpy
            # todo:
            ####################################################################################################
            modified_input = torch.zeros((batch_size,3,self.width_height,self.width_height),device='cuda')
            for idx in range(batch_size):
                print(file_name[idx])
                print(camera_name[idx])
                numpy_rgb = rawpy_tensor2image(raw_image=modified_raw_one_dim[idx], template=file_name[idx],
                                               camera_name=camera_name[idx],patch_size=self.width_height)
                numpy_rgb = numpy_rgb.astype(np.float32) / 255.
                modified_input[idx:idx+1] = torch.from_numpy(np.ascontiguousarray(np.transpose(numpy_rgb, (2, 0, 1)))).contiguous().float()

            tamper_source = torch.zeros((batch_size, 3, self.width_height, self.width_height), device='cuda')
            for idx in range(batch_size):
                numpy_rgb = rawpy_tensor2image(raw_image=input_raw_one_dim[idx], template=file_name[idx],
                                               camera_name=camera_name[idx], patch_size=self.width_height)
                numpy_rgb = numpy_rgb.astype(np.float32) / 255.
                tamper_source[idx:idx + 1] = torch.from_numpy(np.ascontiguousarray(np.transpose(numpy_rgb, (2, 0, 1)))).contiguous().float()
            # raise NotImplementedError("大神搞错了吧？只能支持三种pipeline作为训练")

        ISP_PSNR = self.psnr(self.postprocess(modified_input), self.postprocess(tamper_source)).item()
        modified_input = self.clamp_with_grad(modified_input)
        if tamper_source is not None:
            tamper_source = self.clamp_with_grad(tamper_source)

        locs, cropped, scaled_cropped = self.cropping_mask_generation(
            forward_image=modified_input,  min_rate=0.7, max_rate=1.0, logs=logs)
        h_start, h_end, w_start, w_end = locs
        tamper_source_crop = None
        _, _, tamper_source_crop = self.cropping_mask_generation(forward_image=tamper_source, locs=locs,
                                                                logs=logs)

        percent_range = (0.05, 0.30)
        masks, masks_GT = self.mask_generation(modified_input=modified_input, percent_range=percent_range, logs=logs)

        attacked_forward, masks, masks_GT = self.tampering(
            forward_image=tamper_source_crop, masks=masks, masks_GT=masks_GT,
            modified_input=scaled_cropped, percent_range=percent_range, logs=logs)

        if self.consider_robost:
            if self.using_weak_jpeg_plus_blurring_etc():
                quality_idx = np.random.randint(17, 21)
            else:
                quality_idx = np.random.randint(10, 17)
            attacked_image = self.benign_attacks(attacked_forward=attacked_forward, logs=logs,
                                                 quality_idx=quality_idx)
        else:
            attacked_image = attacked_forward

        self.discriminator_mask.eval()
        pred_resfcn = self.discriminator_mask(attacked_image)
        CE_resfcn = self.bce_with_logit_loss(pred_resfcn, masks_GT)

        info_str = f'[Eval result: RAW_PSNR: {RAW_PSNR}, ISP_PSNR: {ISP_PSNR} CE: {CE_resfcn.item()} ] '
        print(info_str)

        images = stitch_images(
            self.postprocess(input_raw),
            self.postprocess(gt_rgb),
            ### RAW2RAW
            self.postprocess(modified_raw),
            self.postprocess(10 * torch.abs(modified_raw - input_raw)),
            ### RAW2RGB
            self.postprocess(modified_input),
            self.postprocess(tamper_source),
            self.postprocess(10 * torch.abs(modified_input - tamper_source)),
            ### tampering and benign attack
            self.postprocess(attacked_forward),
            self.postprocess(attacked_image),
            self.postprocess(10 * torch.abs(attacked_forward - attacked_image)),
            ### tampering detection
            self.postprocess(masks_GT),
            self.postprocess(torch.sigmoid(pred_resfcn)),
            img_per_row=1
        )

        name = f"{self.out_space_storage}/images/{self.task_name}/{str(self.global_step).zfill(5)}" \
               f"_3_ {str(self.rank)}_eval.png"
        print(f'Bayer: {bayer_pattern}. Saving sample {name}')
        images.save(name)

    def evaluate_with_unseen_isp_pipelines(self, ):
        ####################################################################################################
        # todo: updating the training stage
        # todo:
        ####################################################################################################
        pass

    def visualize_raw(self, raw_to_raw_tensor, bayer_pattern, white_balance=None, eval=False):
        batch_size, height_width = raw_to_raw_tensor.shape[0], raw_to_raw_tensor.shape[2]
        # 两个相机都是RGGB
        # im = np.expand_dims(raw, axis=2)
        # if self.kernel_RAW_k0.ndim!=4:
        #     self.kernel_RAW_k0 = self.kernel_RAW_k0.unsqueeze(0).repeat(batch_size,1,1,1)
        #     self.kernel_RAW_k1 = self.kernel_RAW_k1.unsqueeze(0).repeat(batch_size, 1, 1, 1)
        #     self.kernel_RAW_k2 = self.kernel_RAW_k2.unsqueeze(0).repeat(batch_size, 1, 1, 1)
        #     self.kernel_RAW_k3 = self.kernel_RAW_k3.unsqueeze(0).repeat(batch_size, 1, 1, 1)
        #     print(f"visualize_raw is inited. Current shape {self.kernel_RAW_k0.shape}")
        out_tensor = None
        for idx in range(batch_size):

            used_kernel = getattr(self, f"kernel_RAW_k{bayer_pattern[idx]}")
            v_im = raw_to_raw_tensor[idx:idx+1].repeat(1,3,1,1) * used_kernel

            if white_balance is not None:
                # v_im (1,3,512,512) white_balance (1,3)
                # print(white_balance[idx:idx+1].unsqueeze(2).unsqueeze(3).shape)
                # print(v_im.shape)
                v_im = v_im * white_balance[idx:idx+1].unsqueeze(2).unsqueeze(3)

            out_tensor = v_im if out_tensor is None else torch.cat((out_tensor, v_im), dim=0)

        return out_tensor.float() #.half() if not eval else out_tensor

    def pack_raw(self, raw_to_raw_tensor):
        # 两个相机都是RGGB
        batch_size, num_channels, height_width = raw_to_raw_tensor.shape[0], raw_to_raw_tensor.shape[1], raw_to_raw_tensor.shape[2]

        H, W = raw_to_raw_tensor.shape[2], raw_to_raw_tensor.shape[3]
        R = raw_to_raw_tensor[:,:, 0:H:2, 0:W:2]
        Gr = raw_to_raw_tensor[:,:, 0:H:2, 1:W:2]
        Gb = raw_to_raw_tensor[:,:, 1:H:2, 0:W:2]
        B = raw_to_raw_tensor[:,:, 1:H:2, 1:W:2]
        G_avg = (Gr + Gb) / 2
        out = torch.cat((R, G_avg, B), dim=1)
        print(out.shape)
        out = Functional.interpolate(
            out,
            size=[height_width, height_width],
            mode='bilinear')
        return out

    def cropping_mask_generation(self, forward_image, locs=None, min_rate=0.6, max_rate=1.0, logs=None):
        ####################################################################################################
        # todo: cropping
        # todo: cropped: original-sized cropped image, scaled_cropped: resized cropped image, masks, masks_GT
        ####################################################################################################
        # batch_size, height_width = self.real_H.shape[0], self.real_H.shape[2]
        # masks_GT = torch.ones_like(self.canny_image)

        self.height_ratio = min_rate + (max_rate - min_rate) * np.random.rand()
        self.width_ratio = min_rate + (max_rate - min_rate) * np.random.rand()

        self.height_ratio = min(self.height_ratio, self.width_ratio + 0.2)
        self.width_ratio = min(self.width_ratio, self.height_ratio + 0.2)

        if locs==None:
            h_start, h_end, w_start, w_end = self.crop.get_random_rectangle_inside(forward_image.shape,
                                                                                   self.height_ratio,
                                                                                   self.width_ratio)
        else:
            h_start, h_end, w_start, w_end = locs
        # masks_GT[:, :, h_start: h_end, w_start: w_end] = 0
        # masks = masks_GT.repeat(1, 3, 1, 1)

        cropped = forward_image[:, :, h_start: h_end, w_start: w_end]

        scaled_cropped = Functional.interpolate(
            cropped,
            size=[forward_image.shape[2], forward_image.shape[3]],
            mode='bilinear')
        scaled_cropped = self.clamp_with_grad(scaled_cropped)

        return (h_start, h_end, w_start, w_end), cropped, scaled_cropped #, masks, masks_GT

    def tamper_based_augmentation(self, modified_input, modified_canny, masks, masks_GT, logs):
        # tamper-based data augmentation
        batch_size, height_width = modified_input.shape[0], modified_input.shape[2]
        for imgs in range(batch_size):
            if imgs % 3 != 2:
                modified_input[imgs, :, :, :] = (
                            modified_input[imgs, :, :, :] * (1 - masks[imgs, :, :, :]) + self.previous_images[imgs, :,
                                                                                         :, :] * masks[imgs, :, :,
                                                                                                 :]).clone().detach()
                modified_canny[imgs, :, :, :] = (
                            modified_canny[imgs, :, :, :] * (1 - masks_GT[imgs, :, :, :]) + self.previous_canny[imgs, :,
                                                                                            :, :] * masks_GT[imgs, :, :,
                                                                                                    :]).clone().detach()

        return modified_input, modified_canny

    def mask_generation(self, modified_input, percent_range, logs):
        batch_size, height_width = modified_input.shape[0], modified_input.shape[2]
        masks_GT = torch.zeros(batch_size, 1, self.real_H.shape[2], self.real_H.shape[3]).cuda()
        ## THE RECOVERY STAGE WILL ONLY WORK UNDER LARGE TAMPERING
        ## TO LOCALIZE SMALL TAMPERING, WE ONLY UPDATE LOCALIZER NETWORK

        for imgs in range(batch_size):
            if imgs % 3 == 2:
                ## copy-move will not be too large
                percent_range = (0.00, 0.15)
            masks_origin, _ = self.generate_stroke_mask(
                [self.real_H.shape[2], self.real_H.shape[3]], percent_range=percent_range)
            masks_GT[imgs, :, :, :] = masks_origin.cuda()
        masks = masks_GT.repeat(1, 3, 1, 1)

        # masks is just 3-channel-version masks_GT
        return masks, masks_GT

    def forward_image_generation(self, modified_input, modified_canny, logs):
        # batch_size, height_width = self.real_H.shape[0], self.real_H.shape[2]
        forward_stuff = self.netG(x=torch.cat((modified_input, modified_canny), dim=1))
        forward_stuff = self.clamp_with_grad(forward_stuff)
        forward_image, forward_null = forward_stuff[:, :3, :, :], forward_stuff[:, 3:, :, :]
        psnr_forward = self.psnr(self.postprocess(modified_input), self.postprocess(forward_image)).item()

        return forward_image, forward_null, psnr_forward

    def tampering(self, forward_image, masks, masks_GT, modified_input, percent_range, logs):
        batch_size, height_width = forward_image.shape[0], forward_image.shape[2]
        ####### Tamper ###############
        # attacked_forward = torch.zeros_like(modified_input)
        # for img_idx in range(batch_size):

        if self.using_splicing():
            ####################################################################################################
            # todo: splicing
            # todo: invISP
            ####################################################################################################
            attacked_forward = modified_input * (1 - masks) \
                                                    + self.previous_previous_images * masks
            # attack_name = "splicing"

        elif self.using_copy_move():
            ####################################################################################################
            # todo: copy-move
            # todo: invISP
            ####################################################################################################
            lower_bound_percent = percent_range[0] + (percent_range[1] - percent_range[0]) * np.random.rand()
            tamper = modified_input.clone().detach()
            x_shift, y_shift, valid, retried, max_valid, mask_buff = 0, 0, 0, 0, 0, None
            while retried<20 and not (valid>lower_bound_percent and (abs(x_shift)>(modified_input.shape[2]/3) or abs(y_shift)>(modified_input.shape[3]/3))):
                x_shift = int((modified_input.shape[2]) * (np.random.rand() - 0.5))
                y_shift = int((modified_input.shape[3]) * (np.random.rand() - 0.5))

                ### two times padding ###
                mask_buff = torch.zeros((masks.shape[0], masks.shape[1],
                                            masks.shape[2] + abs(2 * x_shift),
                                            masks.shape[3] + abs(2 * y_shift))).cuda()

                mask_buff[:, :,
                abs(x_shift) + x_shift:abs(x_shift) + x_shift + modified_input.shape[2],
                abs(y_shift) + y_shift:abs(y_shift) + y_shift + modified_input.shape[3]] = masks

                mask_buff = mask_buff[:, :,
                                    abs(x_shift):abs(x_shift) + modified_input.shape[2],
                                    abs(y_shift):abs(y_shift) + modified_input.shape[3]]

                valid = torch.mean(mask_buff)
                retried += 1
                if valid>=max_valid:
                    max_valid = valid
                    self.mask_shifted = mask_buff
                    self.x_shift, self.y_shift = x_shift, y_shift

            self.tamper_shifted = torch.zeros((modified_input.shape[0], modified_input.shape[1],
                                               modified_input.shape[2] + abs(2 * self.x_shift),
                                               modified_input.shape[3] + abs(2 * self.y_shift))).cuda()
            self.tamper_shifted[:, :, abs(self.x_shift) + self.x_shift: abs(self.x_shift) + self.x_shift + modified_input.shape[2],
            abs(self.y_shift) + self.y_shift: abs(self.y_shift) + self.y_shift + modified_input.shape[3]] = tamper


            self.tamper_shifted = self.tamper_shifted[:, :,
                             abs(self.x_shift): abs(self.x_shift) + modified_input.shape[2],
                             abs(self.y_shift): abs(self.y_shift) + modified_input.shape[3]]

            masks = self.mask_shifted.clone().detach()
            masks = self.clamp_with_grad(masks)
            valid = torch.mean(masks)

            masks_GT = masks[:, :1, :, :]
            attacked_forward = modified_input * (1 - masks) + self.tamper_shifted.clone().detach() * masks
            # del self.tamper_shifted
            # del self.mask_shifted
            # torch.cuda.empty_cache()

        else: #if way_tamper == 0:
            ####################################################################################################
            # todo: simulated inpainting
            # todo: it is important, without protection, though the tampering can be close, it should also be detected.
            ####################################################################################################
            attacked_forward = modified_input * (1 - masks) + forward_image * masks

        attacked_forward = self.clamp_with_grad(attacked_forward)
        # attacked_forward = self.Quantization(attacked_forward)

        return attacked_forward, masks, masks_GT

    def benign_attacks(self, attacked_forward, quality_idx, logs):
        batch_size, height_width = attacked_forward.shape[0], attacked_forward.shape[2]
        attacked_real_jpeg = torch.rand_like(attacked_forward).cuda()

        if self.using_gaussian_blur():
            blurring_layer = self.gaussian_blur
        elif self.using_median_blur():
            blurring_layer = self.median_blur
        elif self.using_resizing():
            blurring_layer = self.resize
        else:
            blurring_layer = self.identity

        quality = int(quality_idx * 5)

        jpeg_layer_after_blurring = self.jpeg_simulate[quality_idx - 10][0] if quality < 100 else self.identity
        attacked_real_jpeg_simulate = self.Quantization(
            self.clamp_with_grad(jpeg_layer_after_blurring(blurring_layer(attacked_forward))))
        if self.using_jpeg_simulation_only():
            attacked_image = attacked_real_jpeg_simulate
        else:  # if self.global_step%5==3:
            for idx_atkimg in range(batch_size):
                grid = attacked_forward[idx_atkimg]
                realworld_attack = self.real_world_attacking_on_ndarray(grid, quality)
                attacked_real_jpeg[idx_atkimg:idx_atkimg + 1] = realworld_attack

            attacked_real_jpeg = attacked_real_jpeg.clone().detach()
            attacked_image = attacked_real_jpeg_simulate + (
                        attacked_real_jpeg - attacked_real_jpeg_simulate).clone().detach()

        # error_scratch = attacked_real_jpeg - attacked_forward
        # l_scratch = self.l1_loss(error_scratch, torch.zeros_like(error_scratch).cuda())
        # logs.append(('SCRATCH', l_scratch.item()))
        return attacked_image

    def benign_attacks_without_simulation(self, forward_image, quality_idx, logs):
        batch_size, height_width = forward_image.shape[0], forward_image.shape[2]
        attacked_real_jpeg = torch.rand_like(forward_image).cuda()

        quality = int(quality_idx * 5)

        for idx_atkimg in range(batch_size):
            grid = forward_image[idx_atkimg]
            realworld_attack = self.real_world_attacking_on_ndarray(grid, quality)
            attacked_real_jpeg[idx_atkimg:idx_atkimg + 1] = realworld_attack

        return attacked_real_jpeg

    def real_world_attacking_on_ndarray(self, grid, qf_after_blur, index=None):
        # batch_size, height_width = self.real_H.shape[0], self.real_H.shape[2]
        if index is None:
            index = self.global_step % 5
        if index == 0:
            grid = self.resize(grid.unsqueeze(0))[0]
        ndarr = grid.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).contiguous().to('cpu', torch.uint8).numpy()
        if index == 1:
            kernel_list = [3,5,7]
            kernel = random.choice(kernel_list)
            realworld_attack = cv2.GaussianBlur(ndarr, (kernel, kernel), 0)
        elif index == 2:
            kernel_list = [3, 5, 7]
            kernel = random.choice(kernel_list)
            realworld_attack = cv2.medianBlur(ndarr, kernel)
        else:
            realworld_attack = ndarr

        if qf_after_blur != 100:
            _, realworld_attack = cv2.imencode('.jpeg', realworld_attack,
                                               (int(cv2.IMWRITE_JPEG_QUALITY), qf_after_blur))
            realworld_attack = cv2.imdecode(realworld_attack, cv2.IMREAD_UNCHANGED)
        # realworld_attack = data.util.channel_convert(realworld_attack.shape[2], 'RGB', [realworld_attack])[0]
        # realworld_attack = cv2.resize(copy.deepcopy(realworld_attack), (height_width, height_width),
        #                               interpolation=cv2.INTER_LINEAR)
        realworld_attack = realworld_attack.astype(np.float32) / 255.
        realworld_attack = torch.from_numpy(
            np.ascontiguousarray(np.transpose(realworld_attack, (2, 0, 1)))).contiguous().float()
        realworld_attack = realworld_attack.unsqueeze(0).cuda()
        return realworld_attack

    def localization_loss(self, model, attacked_image, forward_image, masks_GT, modified_input, attacked_forward, logs):
        # batch_size, height_width = self.real_H.shape[0], self.real_H.shape[2]
        ### LOCALIZATION
        gen_attacked_train = model(attacked_image)
        # gen_non_attacked = self.localizer(forward_image)
        # gen_not_protected = self.localizer(modified_input)
        # gen_attacked_non_compress = self.localizer(attacked_forward)
        CE_recall = self.bce_with_logit_loss(gen_attacked_train, masks_GT)
        # CE_precision = self.bce_with_logit_loss(gen_non_attacked, torch.zeros_like(masks_GT))
        # CE_valid = self.bce_with_logit_loss(gen_not_protected, torch.ones_like(masks_GT))
        # CE_non_compress = self.bce_with_logit_loss(gen_attacked_non_compress, masks_GT)
        CE = 0
        # CE += 0.2*CE_precision
        CE += CE_recall
        # CE += 0.2*CE_valid
        # CE += (CE_precision + CE_valid + CE_non_compress) / 3

        # return CE, CE_recall, CE_non_compress, CE_precision, CE_valid, gen_attacked_train
        return CE, CE_recall, None, CE_recall, CE_recall, gen_attacked_train, gen_attacked_train, gen_attacked_train

    def recovery_image_generation(self, attacked_image, masks, modified_canny, logs):
        # batch_size, height_width = self.real_H.shape[0], self.real_H.shape[2]
        ## RECOVERY
        tampered_attacked_image = attacked_image * (1 - masks)
        tampered_attacked_image = self.clamp_with_grad(tampered_attacked_image)
        canny_input = torch.zeros_like(modified_canny).cuda()

        reversed_stuff, _ = self.netG(torch.cat((tampered_attacked_image, canny_input), dim=1), rev=True)
        reversed_stuff = self.clamp_with_grad(reversed_stuff)
        reversed_ch1, reversed_ch2 = reversed_stuff[:, :3, :, :], reversed_stuff[:, 3:, :, :]
        reversed_image = reversed_ch1
        reversed_canny = reversed_ch2

        # reversed_image = modified_expand*(1-masks_expand)+reversed_image*masks_expand
        # reversed_canny = canny_expanded*(1-masks_expand[:,:1])+reversed_canny*masks_expand[:,:1]
        # reversed_image_ideal = modified_input*(1-masks)+reversed_image_ideal*masks
        # reversed_canny_ideal = modified_canny * (1 - masks) + reversed_canny_ideal * masks

        return reversed_image, reversed_canny

    def GAN_loss(self, model, reversed_image, reversed_canny, modified_input, logs):
        # batch_size, height_width = self.real_H.shape[0], self.real_H.shape[2]
        gen_input_fake = torch.cat((reversed_image, reversed_canny), dim=1)
        # dis_input_real = modified_input.clone().detach()
        # dis_real = self.discriminator_mask(dis_input_real)  # in: (grayscale(1) + edge(1))
        gen_fake = model(gen_input_fake)
        REV_GAN = self.bce_with_logit_loss(gen_fake, torch.ones_like(gen_fake))  # / torch.mean(masks)
        # gen_style_loss = 0
        # for i in range(len(dis_real_feat)):
        #     gen_style_loss += self.l1_loss(gen_fake_feat[i], dis_real_feat[i].detach())
        # gen_style_loss = gen_style_loss / 5
        # logs.append(('REV_GAN', gen_style_loss.item()))
        # REV_GAN += gen_style_loss
        return REV_GAN

    def GAN_training(self, model, modified_input, modified_canny, reversed_image, reversed_canny, masks_GT, logs):
        dis_input_real = torch.cat((modified_input, modified_canny), dim=1)
        dis_input_fake = torch.cat((reversed_image, reversed_canny), dim=1)
        dis_real = model(dis_input_real)
        dis_fake = model(dis_input_fake)
        dis_real_loss = self.bce_with_logit_loss(dis_real, torch.ones_like(dis_real))
        dis_fake_loss = self.bce_with_logit_loss(dis_fake, 1 - masks_GT)
        dis_loss = (dis_real_loss + dis_fake_loss) / 2
        return dis_loss

    def optimize_parameters_prepare(self, step=None):
        ####################################################################################################
        # todo: Finetuning ISP pipeline and training identity function on RAW2RAW
        # todo: kept frozen are the networks: invISP, mantranet (+2 more)
        # todo: training: RAW2RAW network (which is denoted as KD-JPEG)
        ####################################################################################################


        self.generator.train()
        self.netG.train()
        self.discriminator_mask.train()
        self.localizer.train()


        logs, debug_logs = {}, []

        self.real_H = self.clamp_with_grad(self.real_H)
        batch_size, num_channels, height_width, _ = self.real_H.shape
        lr = self.get_current_learning_rate()
        logs['lr'] = lr

        input_raw = self.real_H.clone().detach()
        # input_raw = self.clamp_with_grad(input_raw)

        gt_rgb = self.label

        if not (self.previous_images is None or self.previous_previous_images is None):

            with torch.enable_grad(): #cuda.amp.autocast():

                ####################################################################################################
                # todo: Generation of protected RAW
                ####################################################################################################
                # modified_raw = self.KD_JPEG(input_raw)
                ####################################################################################################
                # todo: RAW2RGB pipelines
                ####################################################################################################
                modified_input = self.generator(input_raw)

                # RAW_L1 = self.l1_loss(input=modified_raw, target=input_raw)
                # ISP_PSNR = self.psnr(self.postprocess(modified_raw), self.postprocess(input_raw)).item()

                ISP_L1 = self.l1_loss(input=modified_input, target=gt_rgb)
                modified_input = self.clamp_with_grad(modified_input)
                RAW_PSNR = self.psnr(self.postprocess(modified_input), self.postprocess(gt_rgb)).item()

                loss = ISP_L1

                # logs['RAW_PSNR'] = ISP_PSNR
                logs['ISP_PSNR'] = RAW_PSNR
                logs['loss'] = loss.item()

                percent_range = (0.05, 0.30)
                masks, masks_GT = self.mask_generation(percent_range=percent_range, logs=logs)

                attacked_forward = self.tampering(
                    forward_image=gt_rgb, masks=masks, masks_GT=masks_GT,
                    modified_input=gt_rgb, percent_range=percent_range, logs=logs)

                consider_robost = False
                if consider_robost:
                    if self.global_step % 5 in {0, 1, 2}:
                        quality_idx = np.random.randint(19, 21)
                    else:
                        quality_idx = np.random.randint(12, 21)
                    attacked_image = self.benign_attacks(attacked_forward=attacked_forward, logs=logs,
                                                         quality_idx=quality_idx)
                else:
                    attacked_image = attacked_forward

                ####################################################################################################
                # todo: Image Manipulation Detection Network (Downstream task)
                # todo: mantranet: localizer mvssnet: netG resfcn: discriminator
                ####################################################################################################
                _, pred_mvss = self.netG(attacked_image.detach())
                CE_MVSS = self.bce_with_logit_loss(pred_mvss, masks_GT)

                pred_mantra = self.localizer(attacked_image.detach())
                CE_mantra = self.bce_with_logit_loss(pred_mantra, masks_GT)

                pred_resfcn = self.discriminator_mask(attacked_image.detach())
                CE_resfcn = self.bce_with_logit_loss(pred_resfcn, masks_GT)

                logs['CE_MVSS'] = CE_MVSS.item()
                logs['CE_mantra'] = CE_mantra.item()
                logs['CE_resfcn'] = CE_resfcn.item()

            ####################################################################################################
            # todo: STEP: Image Manipulation Detection Network
            # todo: invISP
            ####################################################################################################
            # self.optimizer_KD_JPEG.zero_grad()
            self.optimizer_generator.zero_grad()
            # loss.backward()
            self.scaler_generator.scale(loss).backward()
            if self.train_opt['gradient_clipping']:
                # nn.utils.clip_grad_norm_(self.KD_JPEG.parameters(), 1)
                nn.utils.clip_grad_norm_(self.generator.parameters(), 1)
            # self.optimizer_KD_JPEG.step()
            # self.optimizer_generator.step()
            self.scaler_generator.step(self.optimizer_generator)
            self.scaler_generator.update()

            self.optimizer_G.zero_grad()
            # loss.backward()
            self.scaler_G.scale(CE_MVSS).backward()
            if self.train_opt['gradient_clipping']:
                nn.utils.clip_grad_norm_(self.netG.parameters(), 1)
            # self.optimizer_G.step()
            self.scaler_G.step(self.optimizer_G)
            self.scaler_G.update()

            self.optimizer_localizer.zero_grad()
            # CE_train.backward()
            self.scaler_localizer.scale(CE_mantra).backward()
            if self.train_opt['gradient_clipping']:
                nn.utils.clip_grad_norm_(self.localizer.parameters(), 1)
            # self.optimizer_localizer.step()
            self.scaler_localizer.step(self.optimizer_localizer)
            self.scaler_localizer.update()

            self.optimizer_discriminator_mask.zero_grad()
            # dis_loss.backward()
            self.scaler_discriminator_mask.scale(CE_resfcn).backward()
            if self.train_opt['gradient_clipping']:
                nn.utils.clip_grad_norm_(self.discriminator_mask.parameters(), 1)
            # self.optimizer_discriminator_mask.step()
            self.scaler_discriminator_mask.step(self.optimizer_discriminator_mask)
            self.scaler_discriminator_mask.update()

            ####################################################################################################
            # todo: observation zone
            # todo: invISP
            ####################################################################################################
            # with torch.no_grad():
            #     REVERSE, _ = self.netG(torch.cat((attacked_real_jpeg * (1 - masks),
            #                    torch.zeros_like(modified_canny).cuda()), dim=1), rev=True)
            #     REVERSE = self.clamp_with_grad(REVERSE)
            #     REVERSE = REVERSE[:, :3, :, :]
            #     l_REV = (self.l1_loss(REVERSE * masks_expand, modified_input * masks_expand))
            #     logs.append(('observe', l_REV.item()))

            ####################################################################################################
            # todo: printing the images
            # todo: invISP
            ####################################################################################################
            anomalies = False  # CE_recall.item()>0.5
            if anomalies or self.global_step % 200 == 3 or self.global_step <= 10:
                images = stitch_images(
                    self.postprocess(input_raw),
                    # self.postprocess(modified_raw),
                    # self.postprocess(10 * torch.abs(modified_raw - input_raw)),
                    ### RAW2RGB
                    self.postprocess(modified_input),
                    self.postprocess(gt_rgb),
                    self.postprocess(10 * torch.abs(gt_rgb - modified_input)),
                    ### tampering and benign attack
                    self.postprocess(attacked_forward),
                    self.postprocess(attacked_image),
                    self.postprocess(10 * torch.abs(attacked_forward - attacked_image)),
                    ### tampering detection
                    self.postprocess(masks_GT),
                    self.postprocess(torch.sigmoid(pred_mvss)),
                    # self.postprocess(10 * torch.abs(masks_GT - torch.sigmoid(pred_mvss))),
                    # self.postprocess(torch.sigmoid(pred_mantra)),
                    # self.postprocess(10 * torch.abs(masks_GT - torch.sigmoid(pred_mantra))),
                    # self.postprocess(torch.sigmoid(pred_resfcn)),
                    # self.postprocess(10 * torch.abs(masks_GT - torch.sigmoid(pred_resfcn))),
                    img_per_row=1
                )

                name = f"{self.out_space_storage}/images/{self.task_name}/{str(self.global_step).zfill(5)}" \
                           f"_3_ {str(self.rank)}.png"
                print('\nsaving sample ' + name)
                images.save(name)

        ####################################################################################################
        # todo: updating the training stage
        # todo:
        ####################################################################################################
        ######## Finally ####################
        if self.global_step % 1000 == 999 or self.global_step == 9:
            if self.rank == 0:
                print('Saving models and training states.')
                self.save(self.global_step, folder='model', network_list=self.network_list)
        if self.real_H is not None:
            if self.previous_images is not None:
                self.previous_previous_images = self.previous_images.clone().detach()
            self.previous_images = self.label
        self.global_step = self.global_step + 1

        # print(logs)
        # print(debug_logs)
        return logs, debug_logs


    def is_image_file(self, filename):
        return any(filename.endswith(extension) for extension in self.IMG_EXTENSIONS)

    def get_paths_from_images(self, path):
        '''get image path list from image folder'''
        assert os.path.isdir(path), '{:s} is not a valid directory'.format(path)
        images = []
        for dirpath, _, fnames in sorted(os.walk(path)):
            for fname in sorted(fnames):
                if self.is_image_file(fname):
                    # img_path = os.path.join(dirpath, fname)
                    images.append((path, dirpath[len(path) + 1:], fname))
        assert images, '{:s} has no valid image file'.format(path)
        return images

    def print_individual_image(self, cropped_GT, name):
        for image_no in range(cropped_GT.shape[0]):
            camera_ready = cropped_GT[image_no].unsqueeze(0)
            torchvision.utils.save_image((camera_ready * 255).round() / 255,
                                         name, nrow=1, padding=0, normalize=False)

    def load_image(self, path, readimg=False, Height=608, Width=608, grayscale=False):
        import data.util as util
        GT_path = path

        img_GT = util.read_img(GT_path)

        # change color space if necessary
        # img_GT = util.channel_convert(img_GT.shape[2], 'RGB', [img_GT])[0]
        if grayscale:
            img_GT = rgb2gray(img_GT)

        img_GT = cv2.resize(copy.deepcopy(img_GT), (Width, Height), interpolation=cv2.INTER_LINEAR)
        return img_GT

    def img_random_crop(self, img_GT, Height=608, Width=608, grayscale=False):
        # # randomly crop
        # H, W = img_GT.shape[0], img_GT.shape[1]
        # rnd_h = random.randint(0, max(0, H - Height))
        # rnd_w = random.randint(0, max(0, W - Width))
        #
        # img_GT = img_GT[rnd_h:rnd_h + Height, rnd_w:rnd_w + Width, :]
        #
        # orig_height, orig_width, _ = img_GT.shape
        # H, W = img_GT.shape[0], img_GT.shape[1]

        # BGR to RGB, HWC to CHW, numpy to tensor
        if not grayscale:
            img_GT = img_GT[:, :, [2, 1, 0]]
            img_GT = torch.from_numpy(
                np.ascontiguousarray(np.transpose(img_GT, (2, 0, 1)))).contiguous().float()
        else:
            img_GT = self.image_to_tensor(img_GT)

        return img_GT.cuda()

    def tensor_to_image(self, tensor):

        tensor = tensor * 255.0
        image = tensor.permute(1, 2, 0).detach().cpu().numpy()
        # image = tensor.permute(0,2,3,1).detach().cpu().numpy()
        return np.clip(image, 0, 255).astype(np.uint8)

    def tensor_to_image_batch(self, tensor):

        tensor = tensor * 255.0
        image = tensor.permute(0, 2, 3, 1).detach().cpu().numpy()
        # image = tensor.permute(0,2,3,1).detach().cpu().numpy()
        return np.clip(image, 0, 255).astype(np.uint8)

    def postprocess(self, img):
        # [0, 1] => [0, 255]
        img = img * 255.0
        img = img.permute(0, 2, 3, 1)
        return img.int()

    def image_to_tensor(self, img):
        img = Image.fromarray(img)
        img_t = F.to_tensor(np.asarray(img)).float()
        return img_t

    def reload(self, pretrain, network_list=['netG', 'localizer']):
        if 'netG' in network_list:
            load_path_G = pretrain + "_netG.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.netG, strict=True)
                else:
                    print('Did not find model for class [{:s}] ...'.format(load_path_G))

        if 'KD_JPEG' in network_list:
            load_path_G = pretrain + "_KD_JPEG.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.KD_JPEG, strict=False)
                else:
                    print('Did not find model for class [{:s}] ...'.format(load_path_G))

        if 'discriminator_mask' in network_list:
            load_path_G = pretrain + "_discriminator_mask.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.discriminator_mask, strict=False)
                else:
                    print('Did not find model for class [{:s}] ...'.format(load_path_G))

        if 'discriminator' in network_list:
            load_path_G = pretrain + "_discriminator.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.discriminator, strict=False)
                else:
                    print('Did not find momentum model for class [{:s}] ... we load the discriminator_mask instead'.format(load_path_G))
                    load_path_G = pretrain + "_discriminator_mask.pth"
                    print('Loading model for class [{:s}] ...'.format(load_path_G))
                    if os.path.exists(load_path_G):
                        self.load_network(load_path_G, self.discriminator_mask, strict=False)
                    else:
                        print('Did not find model for class [{:s}] ...'.format(load_path_G))

        if 'qf_predict_network' in network_list:
            load_path_G = pretrain + "_qf_predict.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.qf_predict_network, self.opt['path']['strict_load'])
                else:
                    print('Did not find model for class [{:s}] ...'.format(load_path_G))

        if 'localizer' in network_list:
            load_path_G = pretrain + "_localizer.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.localizer, strict=False)
                else:
                    print('Did not find model for class [{:s}] ...'.format(load_path_G))

        if 'generator' in network_list:
            load_path_G = pretrain + "_generator.pth"
            if load_path_G is not None:
                print('Loading model for class [{:s}] ...'.format(load_path_G))
                if os.path.exists(load_path_G):
                    self.load_network(load_path_G, self.generator, strict=True)
                else:
                    print('Did not find model for class [{:s}] ...'.format(load_path_G))

    def save(self, iter_label, folder='model', network_list=['netG', 'localizer']):
        if 'netG' in network_list:
            self.save_network(self.netG, 'netG', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')
        if 'localizer' in network_list:
            self.save_network(self.localizer, 'localizer', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')
        if 'KD_JPEG' in network_list:
            self.save_network(self.KD_JPEG, 'KD_JPEG', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')
        if 'discriminator_mask' in network_list:
            self.save_network(self.discriminator_mask, 'discriminator_mask', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')
        if 'discriminator' in network_list:
            self.save_network(self.discriminator, 'discriminator', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')
        if 'qf_predict_network' in network_list:
            self.save_network(self.qf_predict_network, 'qf_predict', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')
        if 'generator' in network_list:
            self.save_network(self.generator, 'generator', iter_label if self.rank == 0 else 0,
                              model_path=self.out_space_storage + f'/{folder}/{self.task_name}/')

    def generate_stroke_mask(self, im_size, parts=5, parts_square=2, maxVertex=6, maxLength=64, maxBrushWidth=32,
                             maxAngle=360, percent_range=(0.0, 0.25)):
        minVertex, maxVertex = 1, 4
        minLength, maxLength = int(im_size[0] * 0.02), int(im_size[0] * 0.25)
        minBrushWidth, maxBrushWidth = int(im_size[0] * 0.02), int(im_size[0] * 0.25)
        mask = np.zeros((im_size[0], im_size[1]), dtype=np.float32)
        lower_bound_percent = percent_range[0] + (percent_range[1] - percent_range[0]) * np.random.rand()

        while True:
            mask = mask + self.np_free_form_mask(mask, minVertex, maxVertex, minLength, maxLength, minBrushWidth,
                                                 maxBrushWidth,
                                                 maxAngle, im_size[0],
                                                 im_size[1])
            mask = np.minimum(mask, 1.0)
            percent = np.mean(mask)
            if percent >= lower_bound_percent:
                break

        mask = np.maximum(mask, 0.0)
        mask_tensor = torch.from_numpy(mask).contiguous()
        # mask = Image.fromarray(mask)
        # mask_tensor = F.to_tensor(mask).float()

        return mask_tensor, np.mean(mask)

    def np_free_form_mask(self, mask_re, minVertex, maxVertex, minLength, maxLength, minBrushWidth, maxBrushWidth,
                          maxAngle, h, w):
        mask = np.zeros_like(mask_re)
        numVertex = np.random.randint(minVertex, maxVertex + 1)
        startY = np.random.randint(h)
        startX = np.random.randint(w)
        brushWidth = 0
        for i in range(numVertex):
            angle = np.random.randint(maxAngle + 1)
            angle = angle / 360.0 * 2 * np.pi
            if i % 2 == 0:
                angle = 2 * np.pi - angle
            length = np.random.randint(minLength, maxLength + 1)
            brushWidth = np.random.randint(minBrushWidth, maxBrushWidth + 1) // 2 * 2
            nextY = startY + length * np.cos(angle)
            nextX = startX + length * np.sin(angle)
            nextY = np.maximum(np.minimum(nextY, h - 1), 0).astype(np.int)
            nextX = np.maximum(np.minimum(nextX, w - 1), 0).astype(np.int)
            cv2.line(mask, (startY, startX), (nextY, nextX), 1, brushWidth)
            cv2.circle(mask, (startY, startX), brushWidth // 2, 2)
            startY, startX = nextY, nextX
        cv2.circle(mask, (startY, startX), brushWidth // 2, 2)
        return mask

    def get_random_rectangle_inside(self, image_width, image_height, height_ratio_range=(0.1, 0.2),
                                    width_ratio_range=(0.1, 0.2)):

        r_float_height, r_float_width = \
            self.random_float(height_ratio_range[0], height_ratio_range[1]), self.random_float(width_ratio_range[0],
                                                                                               width_ratio_range[1])
        remaining_height = int(np.rint(r_float_height * image_height))
        remaining_width = int(np.rint(r_float_width * image_width))

        if remaining_height == image_height:
            height_start = 0
        else:
            height_start = np.random.randint(0, image_height - remaining_height)

        if remaining_width == image_width:
            width_start = 0
        else:
            width_start = np.random.randint(0, image_width - remaining_width)

        return height_start, height_start + remaining_height, width_start, width_start + remaining_width, r_float_height * r_float_width

    def random_float(self, min, max):
        return np.random.rand() * (max - min) + min

    def F1score(self, predict_image, gt_image, thresh=0.2):
        # gt_image = cv2.imread(src_image, 0)
        # predict_image = cv2.imread(dst_image, 0)
        # ret, gt_image = cv2.threshold(gt_image[0], int(255 * thresh), 255, cv2.THRESH_BINARY)
        # ret, predicted_binary = cv2.threshold(predict_image[0], int(255*thresh), 255, cv2.THRESH_BINARY)
        predicted_binary = self.tensor_to_image(predict_image[0])
        ret, predicted_binary = cv2.threshold(predicted_binary, int(255 * thresh), 255, cv2.THRESH_BINARY)
        gt_image = self.tensor_to_image(gt_image[0, :1, :, :])
        ret, gt_image = cv2.threshold(gt_image, int(255 * thresh), 255, cv2.THRESH_BINARY)

        # print(predicted_binary.shape)

        [TN, TP, FN, FP] = getLabels(predicted_binary, gt_image)
        # print("{} {} {} {}".format(TN,TP,FN,FP))
        F1 = getF1(TP, FP, FN)
        # cv2.imwrite(save_path, predicted_binary)
        return F1, TP


def getLabels(img, gt_img):
    height = img.shape[0]
    width = img.shape[1]
    # TN, TP, FN, FP
    result = [0, 0, 0, 0]
    for row in range(height):
        for column in range(width):
            pixel = img[row, column]
            gt_pixel = gt_img[row, column]
            if pixel == gt_pixel:
                result[(pixel // 255)] += 1
            else:
                index = 2 if pixel == 0 else 3
                result[index] += 1
    return result


def getACC(TN, TP, FN, FP):
    return (TP + TN) / (TP + FP + FN + TN)


def getFPR(TN, FP):
    return FP / (FP + TN)


def getTPR(TP, FN):
    return TP / (TP + FN)


def getTNR(FP, TN):
    return TN / (FP + TN)


def getFNR(FN, TP):
    return FN / (TP + FN)


def getF1(TP, FP, FN):
    return (2 * TP) / (2 * TP + FP + FN)


def getBER(TN, TP, FN, FP):
    return 1 / 2 * (getFPR(TN, FP) + FN / (FN + TP))


if __name__ == '__main__':
    pass
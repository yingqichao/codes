CUDA_VISIBLE_DEVICES=1,2 python -m torch.distributed.launch --master_port 3004 --nproc_per_node=2 train.py \
                                -opt options/train/ISP/train_ISP_my_own_elastic.yml -mode 2 -task_name my_own_elastic -loading_from my_own_elastic \
                                --launcher pytorch

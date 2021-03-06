import argparse
import os
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.autograd import Variable
import torch.nn.functional as F
import numpy as np
import time
from tensorboardX import SummaryWriter
from datasets import find_dataset_def
from models import *
from utils import *
import gc
import sys
import datetime
import ast
from datasets.data_io import *

from third_party.sync_batchnorm import patch_replication_callback
from third_party.sync_batchnorm import convert_model
from third_party.radam import RAdam

cudnn.benchmark = True

parser = argparse.ArgumentParser(description='A Official PyTorch Codebase of PVA-MVSNet')
parser.add_argument('--mode', default='train', help='train, val or test', choices=['train', 'test', 'val', 'evaluate', 'profile'])
parser.add_argument('--model', default='mvsnet', help='select model')

parser.add_argument('--loss', default='mvsnet_loss', help='select loss', choices=['mvsnet_loss', 'mvsnet_loss_l1norm', 'mvsnet_loss_divby_interval'])

parser.add_argument('--fea_net', default='FeatureNet', help='feature extractor network')
parser.add_argument('--cost_net', default='CostRegNet', help='cost volume network')

parser.add_argument('--refine', help='True or False flag, input should be either "True" or "False".',
    type=ast.literal_eval, default=False)
parser.add_argument('--refine_net', default='RefineNet', help='refinement network')

parser.add_argument('--dp_ratio', type=float, default=0.0, help='learning rate')

parser.add_argument('--inverse_depth', help='True or False flag, input should be either "True" or "False".',
    type=ast.literal_eval, default=False)
parser.add_argument('--origin_size', help='True or False flag, input should be either "True" or "False".',
    type=ast.literal_eval, default=False)
parser.add_argument('--save_depth', help='True or False flag, input should be either "True" or "False".',
    type=ast.literal_eval, default=False)
parser.add_argument('--syncbn', help='True or False flag, input should be either "True" or "False".',
    type=ast.literal_eval, default=False)

parser.add_argument('--light_idx', type=int, default=3, help='select while in test')
parser.add_argument('--cost_aggregation', type=int, default=0, help='cost aggregation method, default: 0')
parser.add_argument('--view_num', type=int, default=3, help='training view num setting')

parser.add_argument('--image_scale', type=float, default=0.25, help='pred depth map scale') # 0.5

parser.add_argument('--ngpu', type=int, default=4, help='gpu size')

parser.add_argument('--dataset', default='dtu_yao', help='select dataset')
parser.add_argument('--trainpath', help='train datapath')
parser.add_argument('--testpath', help='test datapath')
parser.add_argument('--trainlist', help='train list')
parser.add_argument('--vallist', help='val list')
parser.add_argument('--testlist', help='test list')

parser.add_argument('--epochs', type=int, default=16, help='number of epochs to train')
parser.add_argument('--lr', type=float, default=0.001, help='learning rate')

parser.add_argument('--loss_w', type=int, default=4, help='number of epochs to train')

parser.add_argument('--lrepochs', type=str, default="10,12,14:2", help='epoch ids to downscale lr and the downscale rate')
parser.add_argument('--wd', type=float, default=0.0, help='weight decay')
parser.add_argument('--lr_scheduler', default='multistep', help='lr_scheduler')
parser.add_argument('--optimizer', default='Adam', help='optimizer')

parser.add_argument('--batch_size', type=int, default=12, help='train batch size')
parser.add_argument('--numdepth', type=int, default=192, help='the number of depth values')
parser.add_argument('--interval_scale', type=float, default=1.06, help='the number of depth values') # 1.01

parser.add_argument('--loadckpt', default=None, help='load a specific checkpoint')
parser.add_argument('--logdir', default='./checkpoints/debug', help='the directory to save checkpoints/logs')
parser.add_argument('--save_dir', default=None, help='the directory to save checkpoints/logs')
parser.add_argument('--resume', action='store_true', help='continue to train the model')

parser.add_argument('--summary_freq', type=int, default=20, help='print and summary frequency')
parser.add_argument('--save_freq', type=int, default=1, help='save checkpoint frequency')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed')


# parse arguments and check
args = parser.parse_args()
if args.resume:
    assert args.mode == "train"
    assert args.loadckpt is None
if args.testpath is None:
    args.testpath = args.trainpath

torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)

# create logger for mode "train" and "testall"
if args.mode == "train":
    if not os.path.isdir(args.logdir):
        os.mkdir(args.logdir)

    current_time_str = str(datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    print("current time", current_time_str)

    print("creating new summary file")
    logger = SummaryWriter(args.logdir)

print("argv:", sys.argv[1:])
print_args(args)

SAVE_DEPTH = args.save_depth
if SAVE_DEPTH:
    if args.save_dir is None:
        sub_dir, ckpt_name = os.path.split(args.loadckpt)
        index = ckpt_name[6:-5]
        save_dir = os.path.join(sub_dir, index)
    else:
        save_dir = args.save_dir
    print(os.path.exists(save_dir), ' exists', save_dir)
    if not os.path.exists(save_dir):
        print('save dir', save_dir)
        os.makedirs(save_dir)

# dataset, dataloader
# args.origin_size only load origin size depth, not modify Camera.txt
MVSDataset = find_dataset_def(args.dataset)
train_dataset = MVSDataset(args.trainpath, args.trainlist, "train", args.view_num, args.numdepth, args.interval_scale, args.inverse_depth, args.origin_size, -1, args.image_scale) # Training with False, Test with inverse_depth
val_dataset = MVSDataset(args.trainpath, args.vallist, "val", 5, args.numdepth, args.interval_scale, args.inverse_depth, args.origin_size, args.light_idx, args.image_scale) #view_num = 5, light_idx = 3
test_dataset = MVSDataset(args.testpath, args.testlist, "test", 5, args.numdepth, args.interval_scale, args.inverse_depth, args.origin_size, args.light_idx, args.image_scale) # use 3
TrainImgLoader = DataLoader(train_dataset, args.batch_size, shuffle=True, num_workers=8, drop_last=True)
ValImgLoader = DataLoader(val_dataset, args.batch_size, shuffle=False, num_workers=4, drop_last=False)
TestImgLoader = DataLoader(test_dataset, args.batch_size, shuffle=False, num_workers=4, drop_last=False)


# model, optimizer
if args.model == 'mvsnet':
    print('use MVSNet')
    model = MVSNet(refine=args.refine, fea_net=args.fea_net, cost_net=args.cost_net,
             refine_net=args.refine_net, origin_size=args.origin_size, cost_aggregation=args.cost_aggregation, dp_ratio=args.dp_ratio, image_scale=args.image_scale)
else: 
    print('input pre-defined model')

if args.syncbn == True:
    print('###########################\n')
    print('convert model with sync bn')
    model = convert_model(model)

if args.mode in ["train", "test", "val", "evaluate"]:
    model = nn.DataParallel(model)
    if args.syncbn == True:
        print('###########################\n')
        print('patch model with sync bn')
        patch_replication_callback(model)
model.cuda()

loss_dict = {'mvsnet_loss':mvsnet_loss, 'mvsnet_loss_l1norm':mvsnet_loss_l1norm, 'mvsnet_loss_divby_interval':mvsnet_loss_divby_interval}
try:
    model_loss = loss_dict[args.loss]
except KeyError:
    raise ValueError('invalid loss func key')

if args.optimizer == 'Adam':
    print('optimizer: Adam \n')
    optimizer = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999), weight_decay=args.wd)
elif args.optimizer == 'RAdam':
    print('optimizer: RAdam !!!! \n')
    optimizer = RAdam(model.parameters(), lr=args.lr, betas=(0.9, 0.999), weight_decay=args.wd)


# load parameters
start_epoch = 0
if (args.mode == "train" and args.resume) or (args.mode == "test" and not args.loadckpt):
    saved_models = [fn for fn in os.listdir(args.logdir) if fn.endswith(".ckpt")]
    saved_models = sorted(saved_models, key=lambda x: int(x.split('_')[-1].split('.')[0]))
    # use the latest checkpoint file
    loadckpt = os.path.join(args.logdir, saved_models[-1])
    print("resuming", loadckpt)
    state_dict = torch.load(loadckpt)
    model.load_state_dict(state_dict['model'])
    optimizer.load_state_dict(state_dict['optimizer'])
    start_epoch = state_dict['epoch'] + 1
elif args.loadckpt:
    # load checkpoint file specified by args.loadckpt
    print("loading model {}".format(args.loadckpt))
    state_dict = torch.load(args.loadckpt)
    model.load_state_dict(state_dict['model'])
print("start at epoch {}".format(start_epoch))

# main function
def train():
    print('run train()')
    if args.lr_scheduler == 'multistep':
        print('lr scheduler: multistep')
        milestones = [int(epoch_idx) for epoch_idx in args.lrepochs.split(':')[0].split(',')]
        lr_gamma = 1 / float(args.lrepochs.split(':')[1])
        lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones, gamma=lr_gamma,
                                                            last_epoch=start_epoch - 1)
    elif args.lr_scheduler == 'cosinedecay':
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=2e-06)

    for epoch_idx in range(start_epoch, args.epochs):
        
        print('Epoch {}/{}:'.format(epoch_idx, args.epochs))
        lr_scheduler.step()
        global_step = len(TrainImgLoader) * epoch_idx

        # training
        for batch_idx, sample in enumerate(TrainImgLoader):
            start_time = time.time()
            global_step = len(TrainImgLoader) * epoch_idx + batch_idx
            do_summary = global_step % args.summary_freq == 0
            if 'High' in args.fea_net  and 'Coarse2Fine' in args.cost_net:
                loss, scalar_outputs, image_outputs = train_sample_coarse2fine(sample, detailed_summary=do_summary)
            else:
                loss, scalar_outputs, image_outputs = train_sample(sample, detailed_summary=do_summary, refine= args.refine)
            
            for param_group in optimizer.param_groups:
                lr = param_group['lr']
            
            if do_summary:
                save_scalars(logger, 'train', scalar_outputs, global_step)
                logger.add_scalar('train/lr', lr, global_step)
                save_images(logger, 'train', image_outputs, global_step)
            del scalar_outputs, image_outputs
            print(
                'Epoch {}/{}, Iter {}/{}, LR {}, train loss = {:.3f}, time = {:.3f}'.format(epoch_idx, args.epochs, batch_idx,
                                                                                     len(TrainImgLoader), lr, loss,
                                                                                     time.time() - start_time))

        # checkpoint
        if (epoch_idx + 1) % args.save_freq == 0:
            torch.save({
                'epoch': epoch_idx,
                'model': model.state_dict(),
                'optimizer': optimizer.state_dict()},
                "{}/model_{:0>6}.ckpt".format(args.logdir, epoch_idx))
        
        # on test dataset
        avg_test_scalars = DictAverageMeter()
        for batch_idx, sample in enumerate(TestImgLoader):
            start_time = time.time()
            global_step = len(TestImgLoader) * epoch_idx + batch_idx
            do_summary = global_step % args.summary_freq == 0
            if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
                loss, scalar_outputs, image_outputs = test_sample_coarse2fine(sample, detailed_summary=do_summary)
            else:    
                loss, scalar_outputs, image_outputs = test_sample(sample, detailed_summary=do_summary, refine=args.refine)
            if do_summary:
                save_scalars(logger, 'test', scalar_outputs, global_step)
                save_images(logger, 'test', image_outputs, global_step)
            avg_test_scalars.update(scalar_outputs)
            del scalar_outputs, image_outputs
            print('Epoch {}/{}, Iter {}/{}, test loss = {:.3f}, time = {:3f}'.format(epoch_idx, args.epochs, batch_idx,
                                                                                     len(TestImgLoader), loss,
                                                                                     time.time() - start_time))
        save_scalars(logger, 'fulltest', avg_test_scalars.mean(), global_step)
        print("avg_test_scalars:", avg_test_scalars.mean())

        # validation
        avg_val_scalars = DictAverageMeter()
        for batch_idx, sample in enumerate(ValImgLoader):
            start_time = time.time()
            global_step = len(ValImgLoader) * epoch_idx + batch_idx
            do_summary = global_step % args.summary_freq == 0
            if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
                loss, scalar_outputs, image_outputs = test_sample_coarse2fine(sample, detailed_summary=do_summary)
            else:    
                loss, scalar_outputs, image_outputs = test_sample(sample, detailed_summary=do_summary, refine=args.refine)
            if do_summary:
                save_scalars(logger, 'val', scalar_outputs, global_step)
                save_images(logger, 'val', image_outputs, global_step)
            avg_val_scalars.update(scalar_outputs)
            del scalar_outputs, image_outputs
            print('Epoch {}/{}, Iter {}/{}, val loss = {:.3f}, time = {:3f}'.format(epoch_idx, args.epochs, batch_idx,
                                                                                     len(ValImgLoader), loss,
                                                                                     time.time() - start_time))
        save_scalars(logger, 'fullval', avg_val_scalars.mean(), global_step)
        print("avg_val_scalars:", avg_val_scalars.mean())
        # gc.collect()
        

def test():
    global SAVE_DEPTH
    global save_dir
    print('Phase: test \n')
    avg_test_scalars = DictAverageMeter()
    for batch_idx, sample in enumerate(TestImgLoader):
        start_time = time.time()
        if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
            loss, scalar_outputs, image_outputs = test_sample_coarse2fine(sample, detailed_summary=True)
        else:    
            loss, scalar_outputs, image_outputs = test_sample(sample, detailed_summary=True, refine=args.refine)

        avg_test_scalars.update(scalar_outputs)

        if SAVE_DEPTH:
            if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
                depth_est = image_outputs['depth_est0']
                prob_map_est = image_outputs['photometric_confidence']
            else:    
                depth_est = image_outputs['depth_est']
                prob_map_est = image_outputs['photometric_confidence']
            depth_name = sample['name']
            for j in range(0, len(depth_name)):
                name_split = str.split(depth_name[j], '/')
                sub_dir = os.path.join(save_dir, name_split[-2])
                if not os.path.exists(sub_dir):
                    print('make dir: ', sub_dir)
                    os.makedirs(sub_dir)
                save_depth_path = os.path.join(sub_dir, 'init_'+name_split[-1])
                save_depth_png_path = os.path.join(sub_dir, 'init_'+name_split[-1][:-3]+'png')
                save_prob_path = os.path.join(sub_dir, 'prob_'+name_split[-1])

                save_pfm(save_depth_path, depth_est[j].detach().cpu().numpy())
                save_pfm(save_prob_path, prob_map_est[j].detach().cpu().numpy())
                
        if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
            print('Iter {}/{}, test loss = {:.3f}, time = {:3f}, ame = {:3f}, thres2mm = {:3f}, thres4mm = {:3f}, thres8mm = {:3f}'.format(batch_idx, len(TestImgLoader), loss,
                                                                    time.time() - start_time, scalar_outputs["abs_depth_error0"], scalar_outputs["thres2mm_error0"], 
                                                                    scalar_outputs["thres4mm_error0"], scalar_outputs["thres8mm_error0"]))
        else:
            print('Iter {}/{}, test loss = {:.3f}, time = {:3f}, ame = {:3f}, thres2mm = {:3f}, thres4mm = {:3f}, thres8mm = {:3f}'.format(batch_idx, len(TestImgLoader), loss,
                                                                    time.time() - start_time, scalar_outputs["abs_depth_error"], scalar_outputs["thres2mm_error"], 
                                                                    scalar_outputs["thres4mm_error"], scalar_outputs["thres8mm_error"]))
        del scalar_outputs, image_outputs

        if batch_idx % 100 == 0:
            print("Iter {}/{}, test results = {}".format(batch_idx, len(TestImgLoader), avg_test_scalars.mean()))
    print("avg_test_scalars:", avg_test_scalars.mean())

def val():
    global SAVE_DEPTH
    global save_dir
    print('Phase: Val \n')
    avg_test_scalars = DictAverageMeter()
    for batch_idx, sample in enumerate(ValImgLoader):
        start_time = time.time()
        if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
            loss, scalar_outputs, image_outputs = test_sample_coarse2fine(sample, detailed_summary=True)
        else:    
            loss, scalar_outputs, image_outputs = test_sample(sample, detailed_summary=True, refine=args.refine)
        avg_test_scalars.update(scalar_outputs)

        if SAVE_DEPTH:
            if 'High' in args.fea_net and 'Coarse2Fine' in args.cost_net :
                depth_est = image_outputs['depth_est0']
                prob_map_est = image_outputs['photometric_confidence']
            else:    
                depth_est = image_outputs['depth_est']
                prob_map_est = image_outputs['photometric_confidence']
            depth_name = sample['name']
            for j in range(0, len(depth_name)):
                name_split = str.split(depth_name[j], '/')
                sub_dir = os.path.join(save_dir, name_split[-2])
                if not os.path.exists(sub_dir):
                    print('make dir: ', sub_dir)
                    os.makedirs(sub_dir)
                save_depth_path = os.path.join(sub_dir, 'init_'+name_split[-1])
                save_depth_png_path = os.path.join(sub_dir, 'init_'+name_split[-1][:-3]+'png')
                
                save_prob_path = os.path.join(sub_dir, 'prob_'+name_split[-1])

                save_pfm(save_depth_path, depth_est[j].detach().cpu().numpy())
                save_pfm(save_prob_path, prob_map_est[j].detach().cpu().numpy())
                
        del scalar_outputs, image_outputs

        if batch_idx % 100 == 0:
            print("Iter {}/{}, test results = {}".format(batch_idx, len(ValImgLoader), avg_test_scalars.mean()))
    print("avg_val_scalars:", avg_test_scalars.mean())

def evaluate():
    print('Phase: evaluate \n')
    avg_test_scalars = DictAverageMeter()
    for batch_idx, sample in enumerate(ValImgLoader):
        start_time = time.time()
        loss, scalar_outputs, image_outputs = test_load_sample(sample, detailed_summary=True)
        avg_test_scalars.update(scalar_outputs)
        print('Iter {}/{}, test loss = {:.3f}, time = {:3f}, ame = {:3f}, thres2mm = {:3f}, thres4mm = {:3f}, thres8mm = {:3f}'.format(batch_idx, len(ValImgLoader), loss,
                                                                    time.time() - start_time, scalar_outputs["abs_depth_error"], scalar_outputs["thres2mm_error"], 
                                                                    scalar_outputs["thres4mm_error"], scalar_outputs["thres8mm_error"]))
        del scalar_outputs, image_outputs                                                            
        if batch_idx % 100 == 0:
            print("Iter {}/{}, test results = {}".format(batch_idx, len(ValImgLoader), avg_test_scalars.mean()))
    print("avg_test_scalars:", avg_test_scalars.mean())


def train_sample(sample, detailed_summary=False, refine=False):
    model.train()
    optimizer.zero_grad()

    sample_cuda = tocuda(sample)
    depth_gt = sample_cuda["depth"]
    mask = sample_cuda["mask"]
    depth_interval = sample_cuda["depth_interval"]
    outputs = model(sample_cuda["imgs"], sample_cuda["proj_matrices"], sample_cuda["depth_values"])

    depth_est = outputs["depth"]

    if args.loss == 'mvsnet_loss_divby_interval':
        loss = model_loss(depth_est, depth_gt, mask, depth_interval)
    else:
        loss = model_loss(depth_est, depth_gt, mask)
    if refine:
        if args.image_scale == 0.5:
            assert 'Scale2' in args.fea_net
            half_depth_gt = outputs['half_depth']
            if args.refine_kind == 0:
                refine_depth_est = outputs["refined_depth"]
                if args.loss == 'mvsnet_loss_divby_interval':
                    refine_loss = model_loss(refine_depth_est, half_depth_gt, mask, depth_interval)
                else:
                    refine_loss = model_loss(refine_depth_est, half_depth_gt, mask)
                init_loss = loss
                loss = args.rw * init_loss + refine_loss
            elif args.refine_kind == 1:
                refine_depth_est = outputs["refined_depth"]
                if args.loss == 'mvsnet_loss_divby_interval':
                    refine_loss = model_loss(refine_depth_est, half_depth_gt, mask, depth_interval)
                else:
                    refine_loss = model_loss(refine_depth_est, half_depth_gt, mask)
                init_loss = loss
                loss = init_loss + args.rw * refine_loss
        else:
            if args.refine_kind == 0:
                refine_depth_est = outputs["refined_depth"]
                if args.loss == 'mvsnet_loss_divby_interval':
                    refine_loss = model_loss(refine_depth_est, depth_gt, mask, depth_interval)
                else:
                    refine_loss = model_loss(refine_depth_est, depth_gt, mask)
                init_loss = loss
                loss = args.rw * init_loss + refine_loss
            elif args.refine_kind == 1:
                refine_depth_est = outputs["refined_depth"]
                if args.loss == 'mvsnet_loss_divby_interval':
                    refine_loss = model_loss(refine_depth_est, depth_gt, mask, depth_interval)
                else:
                    refine_loss = model_loss(refine_depth_est, depth_gt, mask)
                init_loss = loss
                loss = init_loss + args.rw * refine_loss

    loss.backward()
    optimizer.step()

    scalar_outputs = {"loss": loss}
    image_outputs = {"depth_est": depth_est * mask, "depth_gt": sample["depth"],
                     "ref_img": sample["imgs"][:, 0],
                     "mask": sample["mask"]}
    if detailed_summary:
        image_outputs["errormap"] = (depth_est - depth_gt).abs() * mask
        scalar_outputs["abs_depth_error"] = AbsDepthError_metrics(depth_est, depth_gt, mask > 0.5)
        scalar_outputs["thres2mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 2)
        scalar_outputs["thres4mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 4)
        scalar_outputs["thres8mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 8)

    if refine:
         scalar_outputs["init_loss"] = init_loss
         scalar_outputs["refine_loss"] = refine_loss
         image_outputs["refine_depth_est"] = refine_depth_est * mask
         if detailed_summary:
            image_outputs["refine_errormap"] = (refine_depth_est - depth_gt).abs() * mask
            scalar_outputs["refine_abs_depth_error"] = AbsDepthError_metrics(refine_depth_est, depth_gt, mask > 0.5)
            scalar_outputs["refine_thres2mm_error"] = Thres_metrics(refine_depth_est, depth_gt, mask > 0.5, 2)
            scalar_outputs["refine_thres4mm_error"] = Thres_metrics(refine_depth_est, depth_gt, mask > 0.5, 4)
            scalar_outputs["refine_thres8mm_error"] = Thres_metrics(refine_depth_est, depth_gt, mask > 0.5, 8)
       
    return tensor2float(loss), tensor2float(scalar_outputs), image_outputs

def train_sample_coarse2fine(sample, detailed_summary=False):
    model.train()
    optimizer.zero_grad()
    
    sample_cuda = tocuda(sample)
    depth_gt = sample_cuda["depth"]
    mask = sample_cuda["mask"]
    depth_interval = sample_cuda["depth_interval"]
    depth_min = 450
    ndepths = 192
    depth_interval = depth_interval[0]

    outputs = model(sample_cuda["imgs"], sample_cuda["proj_matrices"], sample_cuda["depth_values"])

    depth_est = outputs["depth"]
    scale = [1, 0.5, 0.25, 0.125] 
    
    if args.loss_w == 1:
        loss_w = [1, 0.5, 0.25, 0.125] # 1
    elif args.loss_w == 2:
        loss_w = [1, 0.25, 0.0625, 0.031] # 2
    elif args.loss_w == 3:
        loss_w = [0.8, 0.2, 0.05, 0.025] # # 3 lr = 0.0005 better
    elif args.loss_w == 4:
        loss_w = [0.32, 0.08, 0.02, 0.01] # 4 lr=0.001, 5
    elif args.loss_w == 401:
        loss_w = [0.48, 0.08, 0.02, 0.01] # 4 lr=0.001, 5
    elif args.loss_w == 41:
        loss_w = [0.32, 0.16, 0.04, 0.01] # 4 lr=0.001, 5
    elif args.loss_w == 42:
        loss_w = [0.48, 0.16, 0.04, 0.01] # 4 lr=0.001, 5
    elif args.loss_w == 5:
        loss_w = [1, 0, 0, 0] # 5 baseline
    elif args.loss_w == 6:
        loss_w = [1, 1, 1, 1]

    loss_list = []
    mask_list = []
    depth_gt_list = []
    loss_all = 0
    for i in range(len(scale)):
        if args.origin_size == True and args.image_scale == 0.50 and i != 0:
            s_depth_gt = F.interpolate(depth_gt.unsqueeze(1), scale_factor=scale[i]*0.5, mode='bilinear', align_corners=True).squeeze(1)
        else:
            s_depth_gt = F.interpolate(depth_gt.unsqueeze(1), scale_factor=scale[i], mode='bilinear', align_corners=True).squeeze(1)
        s_mask = (s_depth_gt.type(torch.float32) > (depth_min+depth_interval).type(torch.float32)) & (s_depth_gt.type(torch.float32) < (depth_min+(ndepths-2)*depth_interval).type(torch.float32))
        s_mask = s_mask.type(torch.float32).cuda()
        mask_list.append(s_mask)
        depth_gt_list.append(s_depth_gt)
        if args.loss == 'mvsnet_loss_divby_interval':
            loss = model_loss(depth_est[i], s_depth_gt, s_mask, depth_interval)
        else:
            loss = model_loss(depth_est[i], s_depth_gt, s_mask)
        loss_list.append(loss)
        loss_all += loss_w[i] * loss

    loss_all.backward()
    optimizer.step()

    
    scalar_outputs = {"loss": loss_all}
    image_outputs = {"ref_img": sample["imgs"][:, 0] }

    for i in range(len(scale)):
        scalar_outputs['loss{}'.format(i)] = loss_list[i]
        image_outputs['depth_est{}'.format(i)] = depth_est[i] * mask_list[i]
        image_outputs['depth_gt{}'.format(i)] = depth_gt_list[i] * mask_list[i]
        image_outputs['mask{}'.format(i)] = mask_list[i]
        
        if detailed_summary:
            image_outputs["errormap{}".format(i)] = (depth_est[i] - depth_gt_list[i]).abs() * mask_list[i]
            scalar_outputs["abs_depth_error{}".format(i)] = AbsDepthError_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5)
            scalar_outputs["thres2mm_error{}".format(i)] = Thres_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5, 2)
            scalar_outputs["thres4mm_error{}".format(i)] = Thres_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5, 4)
            scalar_outputs["thres8mm_error{}".format(i)] = Thres_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5, 8)

    return tensor2float(loss), tensor2float(scalar_outputs), image_outputs


@make_nograd_func
def test_sample_coarse2fine(sample, detailed_summary=True):
    model.eval()

    sample_cuda = tocuda(sample)
    depth_gt = sample_cuda["depth"]
    mask = sample_cuda["mask"]
    depth_interval = sample_cuda["depth_interval"]

    #TODO 
    depth_min = 450
    ndepths = 192
    depth_interval = depth_interval[0]

    outputs = model(sample_cuda["imgs"], sample_cuda["proj_matrices"], sample_cuda["depth_values"])
    depth_est = outputs["depth"]
    scale = [1, 0.5, 0.25, 0.125]
    loss_w = [1, 0.5, 0.25, 0.125]
    loss_list = []
    mask_list = []
    depth_gt_list = []
    loss_all = 0
    for i in range(len(scale)):
        s_depth_gt = F.interpolate(depth_gt.unsqueeze(1), scale_factor=scale[i], mode='bilinear', align_corners=True).squeeze(1)
        s_mask = (s_depth_gt.type(torch.float32) > (depth_min+depth_interval).type(torch.float32)) & (s_depth_gt.type(torch.float32) < (depth_min+(ndepths-2)*depth_interval).type(torch.float32))
        s_mask = s_mask.type(torch.float32).cuda()
        mask_list.append(s_mask)
        depth_gt_list.append(s_depth_gt)
        if args.loss == 'mvsnet_loss_divby_interval':
            loss = model_loss(depth_est[i], s_depth_gt, s_mask, depth_interval)
        else:
            loss = model_loss(depth_est[i], s_depth_gt, s_mask)
        loss_list.append(loss)
        loss_all += loss_w[i] * loss
    
    scalar_outputs = {"loss": loss_all}
    image_outputs = {"ref_img": sample["imgs"][:, 0], "photometric_confidence": outputs['photometric_confidence'][0]}

    for i in range(len(scale)):
        scalar_outputs['loss{}'.format(i)] = loss_list[i]
        image_outputs['depth_est{}'.format(i)] = depth_est[i] * mask_list[i]
        image_outputs['depth_gt{}'.format(i)] = depth_gt_list[i] * mask_list[i]
        image_outputs['mask{}'.format(i)] = mask_list[i]
        
        if detailed_summary:
            image_outputs["errormap{}".format(i)] = (depth_est[i] - depth_gt_list[i]).abs() * mask_list[i]

        scalar_outputs["abs_depth_error{}".format(i)] = AbsDepthError_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5)
        scalar_outputs["thres2mm_error{}".format(i)] = Thres_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5, 2)
        scalar_outputs["thres4mm_error{}".format(i)] = Thres_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5, 4)
        scalar_outputs["thres8mm_error{}".format(i)] = Thres_metrics(depth_est[i], depth_gt_list[i], mask_list[i] > 0.5, 8)

    return tensor2float(loss), tensor2float(scalar_outputs), image_outputs

@make_nograd_func
def test_sample(sample, detailed_summary=True, refine=False):
    model.eval()
    
    sample_cuda = tocuda(sample)
    depth_gt = sample_cuda["depth"]
    mask = sample_cuda["mask"]
    depth_interval = sample_cuda["depth_interval"]

    outputs = model(sample_cuda["imgs"], sample_cuda["proj_matrices"], sample_cuda["depth_values"])
    depth_est = outputs["depth"]
    photometric_confidence = outputs['photometric_confidence']

    if args.loss == 'mvsnet_loss_divby_interval':
        loss = model_loss(depth_est, depth_gt, mask, depth_interval)
    else:
        loss = model_loss(depth_est, depth_gt, mask)

    if refine: # using DPSNet refine loss
        rw = 0.7
        refine_depth_est = outputs["refined_depth"]
        if args.loss == 'mvsnet_loss_divby_interval':
            refine_loss = model_loss(refine_depth_est, depth_gt, mask, depth_interval)
        else:
            refine_loss = model_loss(refine_depth_est, depth_gt, mask)
        init_loss = loss
        loss = rw * init_loss + refine_loss

    scalar_outputs = {"loss": loss}
    image_outputs = {"depth_est": depth_est * mask, "photometric_confidence": photometric_confidence * mask, "depth_gt": sample["depth"],
                     "ref_img": sample["imgs"][:, 0],
                     "mask": sample["mask"]}

    if detailed_summary:
        image_outputs["errormap"] = (depth_est - depth_gt).abs() * mask
        
    scalar_outputs["abs_depth_error"] = AbsDepthError_metrics(depth_est, depth_gt, mask > 0.5)
    scalar_outputs["thres2mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 2)
    scalar_outputs["thres4mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 4)
    scalar_outputs["thres8mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 8)

    if refine:
        scalar_outputs["init_loss"] = init_loss
        scalar_outputs["refine_loss"] = refine_loss
        image_outputs["refine_depth_est"] = refine_depth_est * mask
        if detailed_summary:
            image_outputs["refine_errormap"] = (refine_depth_est - depth_gt).abs() * mask
        scalar_outputs["refine_abs_depth_error"] = AbsDepthError_metrics(refine_depth_est, depth_gt, mask > 0.5)
        scalar_outputs["refine_thres2mm_error"] = Thres_metrics(refine_depth_est, depth_gt, mask > 0.5, 2)
        scalar_outputs["refine_thres4mm_error"] = Thres_metrics(refine_depth_est, depth_gt, mask > 0.5, 4)
        scalar_outputs["refine_thres8mm_error"] = Thres_metrics(refine_depth_est, depth_gt, mask > 0.5, 8)
       

    return tensor2float(loss), tensor2float(scalar_outputs), image_outputs

@make_nograd_func
def test_load_sample(sample, detailed_summary=True):
    model.eval()

    sample_cuda = tocuda(sample)
    depth_gt = sample_cuda["depth"]
    mask = sample_cuda["mask"]
    depth_interval = sample_cuda["depth_interval"]

    depth_name = sample['name']
    depth_est_list = []
    for one_depth_name in depth_name:
        name_split = str.split(one_depth_name, '/')
        sub_dir = os.path.join(save_dir, name_split[-2])
        depth_path = os.path.join(sub_dir, 'init_'+name_split[-1])
        print('load est depth map: ', depth_path)
        depth_est_list.append(np.array(read_pfm(depth_path)[0], dtype=np.float32))
    depth_est = torch.from_numpy(np.stack(depth_est_list, axis=0)).cuda()
    
    if args.loss == 'mvsnet_loss_divby_interval':
        loss = model_loss(depth_est, depth_gt, mask, depth_interval)
    else:
        loss = model_loss(depth_est, depth_gt, mask)

    scalar_outputs = {"loss": loss}
    image_outputs = {"depth_est": depth_est * mask, "depth_gt": sample["depth"],
                     "ref_img": sample["imgs"][:, 0],
                     "mask": sample["mask"]}

    if detailed_summary:
        image_outputs["errormap"] = (depth_est - depth_gt).abs() * mask
        
    scalar_outputs["abs_depth_error"] = AbsDepthError_metrics(depth_est, depth_gt, mask > 0.5)
    scalar_outputs["thres2mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 2)
    scalar_outputs["thres4mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 4)
    scalar_outputs["thres8mm_error"] = Thres_metrics(depth_est, depth_gt, mask > 0.5, 8)

    return tensor2float(loss), tensor2float(scalar_outputs), image_outputs

def profile():
    warmup_iter = 5
    iter_dataloader = iter(TestImgLoader)

    @make_nograd_func
    def do_iteration():
        torch.cuda.synchronize()
        torch.cuda.synchronize()
        start_time = time.perf_counter()
        test_sample(next(iter_dataloader), detailed_summary=True)
        torch.cuda.synchronize()
        end_time = time.perf_counter()
        return end_time - start_time

    for i in range(warmup_iter):
        t = do_iteration()
        print('WarpUp Iter {}, time = {:.4f}'.format(i, t))

    with torch.autograd.profiler.profile(enabled=True, use_cuda=True) as prof:
        for i in range(5):
            t = do_iteration()
            print('Profile Iter {}, time = {:.4f}'.format(i, t))
            time.sleep(0.02)

    if prof is not None:
        # print(prof)
        trace_fn = 'chrome-trace.bin'
        prof.export_chrome_trace(trace_fn)
        print("chrome trace file is written to: ", trace_fn)


if __name__ == '__main__':
    if args.mode == "train":
        train()
    elif args.mode == "test":
        test()
    elif args.mode == "val":
        val()
    elif args.mode == 'evaluate':
        evaluate()
    elif args.mode == "profile":
        profile()

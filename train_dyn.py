from typing import Any, Optional
from pytorch_lightning.utilities.types import STEP_OUTPUT
import torch
from torch import nn
from opt import get_opts
import os
import glob
import imageio
import numpy as np
import cv2
from einops import rearrange
import time

# data
from torch.utils.data import DataLoader
from datasets import dataset_dict
from datasets.ray_utils import axisangle_to_R, get_rays

# models
from kornia.utils.grid import create_meshgrid3d
from models.networks import DynNGP
from models.rendering import render, MAX_SAMPLES

# optimizer, losses
from apex.optimizers import FusedAdam
from torch.optim.lr_scheduler import CosineAnnealingLR
from losses import NeRFLoss

# metrics
from torchmetrics import (
    PeakSignalNoiseRatio, 
    StructuralSimilarityIndexMeasure
)
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity

# pytorch-lightning
import pytorch_lightning
from pytorch_lightning.strategies import DDPStrategy
from pytorch_lightning.strategies import SingleDeviceStrategy
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import TQDMProgressBar, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.loggers import WandbLogger
from lightning_fabric.utilities.distributed import _all_gather_ddp_if_available as new_all_gather_ddp_if_available

from utils import slim_ckpt, load_ckpt

import warnings; warnings.filterwarnings("ignore")

import wandb

def depth2img(depth):
    depth = (depth-depth.min())/(depth.max()-depth.min())
    depth_img = cv2.applyColorMap((depth*255).astype(np.uint8),
                                  cv2.COLORMAP_TURBO)

    return depth_img


class NeRFSystem(LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.save_hyperparameters(hparams)

        self.validation_step_outputs = [[],[]]
        self.test_step_outputs = [[],[]]

        self.warmup_steps = 256
        self.update_interval = 16

        self.loss = NeRFLoss(lambda_distortion=self.hparams.distortion_loss_w)
        self.train_psnr = PeakSignalNoiseRatio(data_range=1)
        self.val_psnr = PeakSignalNoiseRatio(data_range=1)
        self.val_ssim = StructuralSimilarityIndexMeasure(data_range=1)
        if self.hparams.eval_lpips:
            self.val_lpips = LearnedPerceptualImagePatchSimilarity('vgg')
            for p in self.val_lpips.net.parameters():
                p.requires_grad = False

        rgb_act = 'None' if self.hparams.use_exposure else 'Sigmoid'
        self.model = DynNGP(scale=self.hparams.scale, 
                            hparams=hparams,
                            rgb_act=rgb_act)
        G = self.model.grid_size
        self.model.register_buffer('density_grid',
            torch.zeros(self.model.cascades, G**3))
        self.model.register_buffer('grid_coords',
            create_meshgrid3d(G, G, G, False, dtype=torch.int32).reshape(-1, 3))

    def forward(self, batch, split):
        if split=='train':
            poses = self.poses[batch['img_idxs']]
            directions = self.directions[batch['pix_idxs']]
        else:
            poses = batch['pose']
            directions = self.directions

        if self.hparams.optimize_ext:
            dR = axisangle_to_R(self.dR[batch['img_idxs']])
            poses[..., :3] = dR @ poses[..., :3]
            poses[..., 3] += self.dT[batch['img_idxs']]

        rays_o, rays_d = get_rays(directions, poses)

        kwargs = {'test_time': split!='train',
                  'random_bg': self.hparams.random_bg}
        if self.hparams.scale > 0.5:
            kwargs['exp_step_factor'] = 1/256
        if self.hparams.use_exposure:
            kwargs['exposure'] = batch['exposure']

        return render(self.model, rays_o, rays_d, **kwargs)

    def setup(self, stage):
        dataset = dataset_dict[self.hparams.dataset_name]
        kwargs = {'root_dir': self.hparams.root_dir,
                  'downsample': self.hparams.downsample}
        self.train_dataset = dataset(split=self.hparams.split, **kwargs)
        self.train_dataset.batch_size = self.hparams.batch_size
        self.train_dataset.ray_sampling_strategy = self.hparams.ray_sampling_strategy

        self.test_dataset = dataset(split='test', **kwargs)

    def configure_optimizers(self):
        # define additional parameters
        self.register_buffer('directions', self.train_dataset.directions.to(self.device))
        self.register_buffer('poses', self.train_dataset.poses.to(self.device))

        if self.hparams.optimize_ext:
            N = len(self.train_dataset.poses)
            self.register_parameter('dR',
                nn.Parameter(torch.zeros(N, 3, device=self.device)))
            self.register_parameter('dT',
                nn.Parameter(torch.zeros(N, 3, device=self.device)))

        load_ckpt(self.model, self.hparams.weight_path)

        net_params = []
        for n, p in self.named_parameters():
            if n not in ['dR', 'dT']: net_params += [p]

        opts = []
        self.net_opt = FusedAdam(net_params, self.hparams.lr, eps=1e-15)
        opts += [self.net_opt]
        if self.hparams.optimize_ext:
            opts += [FusedAdam([self.dR, self.dT], 1e-6)] # learning rate is hard-coded
        net_sch = CosineAnnealingLR(self.net_opt,
                                    self.hparams.num_epochs-1,
                                    self.hparams.lr*0.01)

        return opts, [net_sch]

    def train_dataloader(self):
        return DataLoader(self.train_dataset,
                          num_workers=16,
                          persistent_workers=True,
                          batch_size=None,
                          pin_memory=True)

    def test_dataloader(self):
        return DataLoader(self.test_dataset,
                          num_workers=1,
                          batch_size=None,
                          pin_memory=True)

    def val_dataloader(self):
        return DataLoader(self.test_dataset,
                          num_workers=1,
                          batch_size=None,
                          pin_memory=True)

    def on_train_start(self):
        self.model.mark_invisible_cells(self.train_dataset.K.to(self.device),
                                        self.poses,
                                        self.train_dataset.img_wh)

    def training_step(self, batch, batch_nb, *args):
        if self.global_step%self.update_interval == 0:
            self.model.update_density_grid(0.01*MAX_SAMPLES/3**0.5,
                                           warmup=self.global_step<self.warmup_steps,
                                           erode=self.hparams.dataset_name=='colmap')

        loss_d = {}
        self.model.subgrid = int(self.global_step)%2
        results = self(batch, split='train')
        loss_d = self.loss(results, batch)
        loss_d["rgb"] += self.loss(results, batch)["rgb"]
        loss_d["opacity"] += self.loss(results, batch)["opacity"]
        if self.hparams.use_exposure:
            zero_radiance = torch.zeros(1, 3, device=self.device)
            unit_exposure_rgb = self.model.log_radiance_to_rgb(zero_radiance,
                                    **{'exposure': torch.ones(1, 1, device=self.device)})
            loss_d['unit_exposure'] = \
                0.5*(unit_exposure_rgb-self.train_dataset.unit_exposure_rgb)**2
        loss = sum(lo.mean() for lo in loss_d.values())

        with torch.no_grad():
            self.train_psnr(results['rgb'], batch['rgb'])
        # self.log('lr', self.net_opt.param_groups[0]['lr'])
        if(self.model.subgrid==0):
            self.log('train0/loss', loss)
            # ray marching samples per ray (occupied space on the ray)
            self.log('train0/rm_s', results['rm_samples']/len(batch['rgb']), True)
            # volume rendering samples per ray (stops marching when transmittance drops below 1e-4)
            # self.log('train/vr_s', results['vr_samples']/len(batch['rgb']), True)
            self.log('train0', results['vr_samples'], True)
            self.log('vr_s0', len(batch['rgb']), True)
            self.log('train0/psnr', self.train_psnr, True)
        else:
            # self.log('lr', self.net_opt.param_groups[0]['lr'])
            self.log('train1/loss', loss)
            # ray marching samples per ray (occupied space on the ray)
            self.log('train1/rm_s', results['rm_samples']/len(batch['rgb']), True)
            # volume rendering samples per ray (stops marching when transmittance drops below 1e-4)
            # self.log('train/vr_s', results['vr_samples']/len(batch['rgb']), True)
            self.log('train1', results['vr_samples'], True)
            self.log('vr_s1', len(batch['rgb']), True)
            self.log('train1/psnr', self.train_psnr, True)

        return loss

    def on_validation_start(self):
        torch.cuda.empty_cache()
        if not self.hparams.no_save_test:
            self.val_dir = f'results/{self.hparams.dataset_name}/{self.hparams.exp_name}'
            os.makedirs(self.val_dir, exist_ok=True)

    def validation_step(self, batch, batch_nb):
        for i in range(2):
            rgb_gt = batch['rgb']
            self.model.subgrid = i
            temp_log = {}
            t_render = time.time()
            result = self(batch, split='test')
            temp_log['render_duration'] = time.time() - t_render
            self.val_psnr(result['rgb'], rgb_gt)
            temp_log['psnr'] = self.val_psnr.compute()
            self.val_psnr.reset()

            w, h = self.train_dataset.img_wh
            rgb_pred = rearrange(result['rgb'], '(h w) c -> 1 c h w', h=h)
            rgb_gt = rearrange(rgb_gt, '(h w) c -> 1 c h w', h=h)
            self.val_ssim(rgb_pred, rgb_gt)
            temp_log['ssim'] = self.val_ssim.compute()
            self.val_ssim.reset()
            if self.hparams.eval_lpips:
                self.val_lpips(torch.clip(rgb_pred*2-1, -1, 1),
                            torch.clip(rgb_gt*2-1, -1, 1))
                temp_log['lpips'] = self.val_lpips.compute()
                self.val_lpips.reset()

            if not self.hparams.no_save_test: # save test image to disk
                idx = batch['img_idxs']
                rgb_pred = rearrange(result['rgb'].cpu().numpy(), '(h w) c -> h w c', h=h)
                rgb_pred = (rgb_pred*255).astype(np.uint8)
                depth = depth2img(rearrange(result['depth'].cpu().numpy(), '(h w) -> h w', h=h))
                imageio.imsave(os.path.join(self.val_dir, f'{idx:03d}.png'), rgb_pred)
                imageio.imsave(os.path.join(self.val_dir, f'{idx:03d}_d.png'), depth)
            self.validation_step_outputs[i].append(temp_log)

    def on_validation_epoch_end(self):
        for i in range(2):
            psnrs = torch.stack([x['psnr'] for x in self.validation_step_outputs[i]])
            mean_psnr = new_all_gather_ddp_if_available(psnrs).mean()
            self.log('test'+str(i)+'/psnr', mean_psnr, True)

            ssims = torch.stack([x['ssim'] for x in self.validation_step_outputs[i]])
            mean_ssim = new_all_gather_ddp_if_available(ssims).mean()
            self.log('test'+str(i)+'/ssim', mean_ssim)

            mean_fps = 1 / np.mean([x['render_duration'] for x in self.validation_step_outputs[i]])
            self.log('test'+str(i)+'/fps', mean_fps, True)

            if self.hparams.eval_lpips:
                lpipss = torch.stack([x['lpips'] for x in self.validation_step_outputs[i]])
                mean_lpips = new_all_gather_ddp_if_available(lpipss).mean()
                self.log('test'+str(i)+'/lpips_vgg', mean_lpips)

    def test_step(self, batch, batch_idx):
        rgb_gt = batch['rgb']
        for i in range(2):
            self.model.subgrid = i
            temp_log = {}
            t_render = time.time()
            result = self(batch, split='test')
            temp_log['render_duration'] = time.time() - t_render
            self.val_psnr(result['rgb'], rgb_gt)
            temp_log['psnr'] = self.val_psnr.compute()
            self.val_psnr.reset()

            w, h = self.train_dataset.img_wh
            rgb_pred = rearrange(result['rgb'], '(h w) c -> 1 c h w', h=h)
            rgb_gt_ss = rearrange(rgb_gt, '(h w) c -> 1 c h w', h=h)
            self.val_ssim(rgb_pred, rgb_gt_ss)
            temp_log['ssim'] = self.val_ssim.compute()
            self.val_ssim.reset()
            if self.hparams.eval_lpips:
                self.val_lpips(torch.clip(rgb_pred*2-1, -1, 1),
                            torch.clip(rgb_gt_ss*2-1, -1, 1))
                temp_log['lpips'] = self.val_lpips.compute()
                self.val_lpips.reset()

            if not self.hparams.no_save_test: # save test image to disk
                idx = batch['img_idxs']
                rgb_pred = rearrange(result['rgb'].cpu().numpy(), '(h w) c -> h w c', h=h)
                rgb_pred = (rgb_pred*255).astype(np.uint8)
                depth = depth2img(rearrange(result['depth'].cpu().numpy(), '(h w) -> h w', h=h))
                imageio.imsave(os.path.join(self.val_dir, f'{idx:03d}.png'), rgb_pred)
                imageio.imsave(os.path.join(self.val_dir, f'{idx:03d}_d.png'), depth)
            self.test_step_outputs[i].append(temp_log)
    
    def on_test_epoch_start(self):
        torch.cuda.empty_cache()
        if not self.hparams.no_save_test:
            self.val_dir = f'results/{self.hparams.dataset_name}/{self.hparams.exp_name}'
            os.makedirs(self.val_dir, exist_ok=True)

        self.register_buffer('directions', self.train_dataset.directions.to(self.device))
        self.register_buffer('poses', self.train_dataset.poses.to(self.device))

        if self.hparams.optimize_ext:
            N = len(self.train_dataset.poses)
            self.register_parameter('dR',
                nn.Parameter(torch.zeros(N, 3, device=self.device)))
            self.register_parameter('dT',
                nn.Parameter(torch.zeros(N, 3, device=self.device)))

    def on_test_epoch_end(self):
        for i in range(2):
            psnrs = torch.stack([x['psnr'] for x in self.test_step_outputs[i]])
            mean_psnr = new_all_gather_ddp_if_available(psnrs).mean()
            self.log('test'+str(i)+'/psnr', mean_psnr, True)

            ssims = torch.stack([x['ssim'] for x in self.test_step_outputs[i]])
            mean_ssim = new_all_gather_ddp_if_available(ssims).mean()
            self.log('test'+str(i)+'/ssim', mean_ssim)

            mean_fps = 1 / np.mean([x['render_duration'] for x in self.test_step_outputs[i]])
            self.log('test'+str(i)+'/fps', mean_fps, True)

            if self.hparams.eval_lpips:
                lpipss = torch.stack([x['lpips'] for x in self.test_step_outputs[i]])
                mean_lpips = new_all_gather_ddp_if_available(lpipss).mean()
                self.log('test'+str(i)+'/lpips_vgg', mean_lpips)

    def get_progress_bar_dict(self):
        # don't show the version number
        items = super().get_progress_bar_dict()
        items.pop("v_num", None)
        return items


if __name__ == '__main__':
    hparams = get_opts()
    if hparams.val_only and (not hparams.ckpt_path):
        raise ValueError('You need to provide a @ckpt_path for validation!')

    if hparams.val_only:
        system = NeRFSystem.load_from_checkpoint(hparams.ckpt_path, strict=False, hparams=hparams)
        wandb_logger = WandbLogger(project=hparams.exp_name, job_type='test')
    else:
        system = NeRFSystem(hparams)
        wandb_logger = WandbLogger(project=hparams.exp_name, job_type='train')
    

    ckpt_cb = ModelCheckpoint(dirpath=f'ckpts/{hparams.dataset_name}/{hparams.exp_name}',
                              filename='{epoch:d}',
                              save_weights_only=True,
                              every_n_epochs=hparams.num_epochs,
                              save_on_train_epoch_end=True,
                              save_top_k=-1)
    callbacks = [ckpt_cb, TQDMProgressBar(refresh_rate=1)]

    logger = TensorBoardLogger(save_dir=f"logs/{hparams.dataset_name}",
                               name=hparams.exp_name,
                               default_hp_metric=False)

    trainer = Trainer(max_epochs=hparams.num_epochs if hparams.num_epochs else 1,
                      check_val_every_n_epoch=hparams.num_epochs if hparams.num_epochs else 1,
                      callbacks=callbacks,
                      logger=wandb_logger,
                      enable_model_summary=False,
                      accelerator='gpu',
                      devices=hparams.num_gpus,
                      strategy="ddp"
                               if hparams.num_gpus>1 else "auto",
                      num_sanity_val_steps=-1 if hparams.val_only else 0,
                      precision=16)

    if hparams.val_only:
        system.model.subgrid = 1
        trainer.test(system)
    else:
        trainer.fit(system)
    # trainer.fit(system, ckpt_path=hparams.ckpt_path)

    if not hparams.val_only: # save slimmed ckpt for the last epoch
        ckpt_ = \
            slim_ckpt(f'ckpts/{hparams.dataset_name}/{hparams.exp_name}/epoch={hparams.num_epochs-1}.ckpt',
                      save_poses=hparams.optimize_ext)
        torch.save(ckpt_, f'ckpts/{hparams.dataset_name}/{hparams.exp_name}/epoch={hparams.num_epochs-1}_slim.ckpt')

    if (not hparams.no_save_test) and \
       hparams.dataset_name=='nsvf' and \
       'Synthetic' in hparams.root_dir: # save video
        imgs = sorted(glob.glob(os.path.join(system.val_dir, '*.png')))
        imageio.mimsave(os.path.join(system.val_dir, 'rgb.mp4'),
                        [imageio.imread(img) for img in imgs[::2]],
                        fps=30, macro_block_size=1)
        imageio.mimsave(os.path.join(system.val_dir, 'depth.mp4'),
                        [imageio.imread(img) for img in imgs[1::2]],
                        fps=30, macro_block_size=1)

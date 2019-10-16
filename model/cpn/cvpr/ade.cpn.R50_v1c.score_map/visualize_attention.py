#!/usr/bin/env python3
# encoding: utf-8
import os
import cv2
import argparse
import numpy as np

import torch
import torch.multiprocessing as mp
import torch.nn.functional as F

from config import config
from utils.pyt_utils import ensure_dir, link_file, load_model, parse_devices
from utils.visualize import print_iou, show_img, set_img_color

from utils.img_utils import pad_image_to_shape
from engine.evaluator import Evaluator
from engine.logger import get_logger
from seg_opr.metric import hist_info, compute_score
from seg_opr.seg_oprs import one_hot

from datasets.ade import ADE
from network import CPNet

logger = get_logger()


class SegEvaluator(Evaluator):
    def func_per_iteration(self, data, device):
        img = data['data']
        label = data['label']
        name = data['fn']
        # label = label - 1

        pred = self.sliding_eval(img, label, name, config.eval_crop_size,
                                 config.eval_stride_rate, device)
        label = label - 1
        hist_tmp, labeled_tmp, correct_tmp = hist_info(config.num_classes,
                                                       pred,
                                                       label)
        results_dict = {'hist': hist_tmp, 'labeled': labeled_tmp,
                        'correct': correct_tmp}

        if self.save_path is not None:
            fn = name + '.png'
            # label = label + 1
            img = np.zeros((pred.shape[0], pred.shape[1], 3))
            img = set_img_color(self.dataset.get_class_colors(), -1, img,
                                pred + 1)
            cv2.imwrite(os.path.join(self.save_path, fn), img)
            logger.info('Save the image ' + fn)

        if self.show_image:
            colors = self.dataset.get_class_colors()
            image = img
            clean = np.zeros(label.shape)
            comp_img = show_img(colors, config.background, image * 255, clean,
                                label,
                                pred)
            cv2.imshow('comp_image', comp_img)
            cv2.waitKey(0)

        return results_dict

    def sliding_eval(self, img, label, name, crop_size, stride_rate, device=None):
        ori_rows, ori_cols, c = img.shape
        processed_pred = np.zeros((ori_rows, ori_cols, self.class_num))

        for s in self.multi_scales:
            img_scale = cv2.resize(img, None, fx=s, fy=s,
                                   interpolation=cv2.INTER_LINEAR)
            new_rows, new_cols, _ = img_scale.shape
            processed_pred += self.scale_process(img_scale, label, name,
                                                 (ori_rows, ori_cols),
                                                 crop_size, stride_rate, device)

        pred = processed_pred.argmax(2)

        return pred

    def scale_process(self, img, label, name, ori_shape, crop_size, stride_rate,
                      device=None):
        new_rows, new_cols, c = img.shape
        long_size = new_cols if new_cols > new_rows else new_rows

        if long_size <= crop_size:
            input_data, margin = self.process_image(img, crop_size)
            label, margin = pad_image_to_shape(label, crop_size,
                                               cv2.BORDER_CONSTANT, value=0)
            score = self.val_func_process(input_data, name, label, device)
            score = score[:, margin[0]:(score.shape[1] - margin[1]),
                    margin[2]:(score.shape[2] - margin[3])]
        else:
            stride = int(np.ceil(crop_size * stride_rate))
            img_pad, margin = pad_image_to_shape(img, crop_size,
                                                 cv2.BORDER_CONSTANT, value=0)
            label, margin = pad_image_to_shape(label, crop_size,
                                               cv2.BORDER_CONSTANT, value=0)

            pad_rows = img_pad.shape[0]
            pad_cols = img_pad.shape[1]
            r_grid = int(np.ceil((pad_rows - crop_size) / stride)) + 1
            c_grid = int(np.ceil((pad_cols - crop_size) / stride)) + 1
            data_scale = torch.zeros(self.class_num, pad_rows, pad_cols).cuda(
                device)
            count_scale = torch.zeros(self.class_num, pad_rows, pad_cols).cuda(
                device)

            for grid_yidx in range(r_grid):
                for grid_xidx in range(c_grid):
                    s_x = grid_xidx * stride
                    s_y = grid_yidx * stride
                    e_x = min(s_x + crop_size, pad_cols)
                    e_y = min(s_y + crop_size, pad_rows)
                    s_x = e_x - crop_size
                    s_y = e_y - crop_size
                    img_sub = img_pad[s_y:e_y, s_x: e_x, :]
                    count_scale[:, s_y: e_y, s_x: e_x] += 1

                    label_sub = label[s_y:e_y, s_x: e_x]

                    input_data, tmargin = self.process_image(img_sub, crop_size)
                    label_sub, _ = pad_image_to_shape(label_sub, crop_size,
                                                      cv2.BORDER_CONSTANT,
                                                      value=0)
                    temp_score = self.val_func_process(input_data, name, label_sub,
                                                       device)
                    temp_score = temp_score[:,
                                 tmargin[0]:(temp_score.shape[1] - tmargin[1]),
                                 tmargin[2]:(temp_score.shape[2] - tmargin[3])]
                    data_scale[:, s_y: e_y, s_x: e_x] += temp_score
            # score = data_scale / count_scale
            score = data_scale
            score = score[:, margin[0]:(score.shape[1] - margin[1]),
                    margin[2]:(score.shape[2] - margin[3])]

        score = score.permute(1, 2, 0)
        data_output = cv2.resize(score.cpu().numpy(),
                                 (ori_shape[1], ori_shape[0]),
                                 interpolation=cv2.INTER_LINEAR)

        return data_output

    def val_func_process(self, input_data, name, label, device=None):
        input_data = np.ascontiguousarray(input_data[None, :, :, :],
                                          dtype=np.float32)
        input_data = torch.FloatTensor(input_data).cuda(device)

        label = np.ascontiguousarray(label[None, :, :],
                                     dtype=np.float32)
        label = torch.FloatTensor(label).cuda(device)

        b, h, w = label.size()
        scaled_gts = F.interpolate((label.view(b, 1, h, w)).float(),
                                   scale_factor=0.125,
                                   mode="nearest")
        b, c, h, w = scaled_gts.size()
        scaled_gts = scaled_gts.view(b, h, w)
        C = config.num_classes + 1
        one_hot_gts = one_hot(scaled_gts, C).view(b, C, -1)
        similarity_gts = torch.bmm(one_hot_gts.permute(0, 2, 1),
                                   one_hot_gts)

        with torch.cuda.device(input_data.get_device()):
            self.val_func.eval()
            self.val_func.to(input_data.get_device())
            with torch.no_grad():
                score = self.val_func(input_data, name, aux_label=similarity_gts)
                # score = F.interpolate(score, scale_factor=8, mode='bilinear',
                #                       align_corners=True)
                score = score[0]

                if self.is_flip:
                    input_data = input_data.flip(-1)
                    score_flip = self.val_func(input_data)
                    score_flip = score_flip[0]
                    score += score_flip.flip(-1)
                score = torch.exp(score)
                # score = score.data

        return score

    def compute_metric(self, results):
        hist = np.zeros((config.num_classes, config.num_classes))
        correct = 0
        labeled = 0
        count = 0
        for d in results:
            hist += d['hist']
            correct += d['correct']
            labeled += d['labeled']
            count += 1

        iu, mean_IU, _, mean_pixel_acc = compute_score(hist, correct,
                                                       labeled)
        result_line = print_iou(iu, mean_pixel_acc,
                                dataset.get_class_names(), True)
        return result_line


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-e', '--epochs', default='last', type=str)
    parser.add_argument('-d', '--devices', default='1', type=str)
    parser.add_argument('-v', '--verbose', default=False, action='store_true')
    parser.add_argument('--show_image', '-s', default=False,
                        action='store_true')
    parser.add_argument('--save_path', '-p', default=None)

    args = parser.parse_args()
    all_dev = parse_devices(args.devices)

    mp_ctx = mp.get_context('spawn')
    network = CPNet(config.num_classes, criterion=None)
    data_setting = {'img_root': config.img_root_folder,
                    'gt_root': config.gt_root_folder,
                    'train_source': config.train_source,
                    'eval_source': config.eval_source}
    dataset = ADE(data_setting, 'val', None)

    with torch.no_grad():
        segmentor = SegEvaluator(dataset, config.num_classes, config.image_mean,
                                 config.image_std, network,
                                 config.eval_scale_array, config.eval_flip,
                                 all_dev, args.verbose, args.save_path,
                                 args.show_image)
        segmentor.run(config.snapshot_dir, args.epochs, config.val_log_file,
                      config.link_val_log_file)

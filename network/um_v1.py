#coding=utf-8
from __future__ import print_function, absolute_import, division


import tensorflow as tf
import numpy as np
from network.slim import scopes, ops, losses, variables
CAM_num = 0
FLAGS = tf.app.flags.FLAGS

_batch_norm_params={'decay':0.99,
                    'epsilon':0.001,
                    'center':True,
                    'scale':True}

# simultaneously regressing the 3D offset and the 2D heatmap
# v0 + dropout on the fully connected layer
TOWER_NAME = 'um_v1'


def standard_group_conv(num_groups, net, num_outputs, kernel_size=3, scope='sgc'):
    n, h, w, c = net.shape

    #with tf.variable_scope(scope):
    with scopes.arg_scope([ops.conv2d],
                          stddev=0.01,
                          activation=tf.nn.relu,
                          batch_norm_params=_batch_norm_params,
                          weight_decay=0.0005,
                          stride=1,
                          padding='SAME'):
        net_splits = tf.split(net, [int(c // num_groups)] * num_groups, axis=-1)
        net = [ops.conv2d(net_split, num_outputs // num_groups, kernel_size) for net_split in net_splits]
        net = tf.concat(net, axis=-1)  # (n, h, w, num_outputs)

    return net

def standard_group_conv_CAM(num_groups, net, num_outputs, kernel_size=3, scope='sgc'):
    n, h, w, c = net.shape
    global CAM_num
    CAM_num = CAM_num+1
    #with tf.variable_scope(scope):
    with scopes.arg_scope([ops.conv2d],
                          stddev=0.01,
                          activation=tf.nn.relu,
                          batch_norm_params=_batch_norm_params,
                          weight_decay=0.0005,
                          stride=1,
                          padding='SAME'):
        net_splits = tf.split(net, [int(c // num_groups)] * num_groups, axis=-1)
        net = [ops.conv2d(net_split, num_outputs // num_groups, kernel_size) for net_split in net_splits]

        weights_initializer = tf.truncated_normal_initializer(stddev=0.01)
        l2_regularizer = losses.l2_regularizer(0.0005)
        learnable_Adj_weights = variables.variable('learnable_adj_weights_' + str(CAM_num),
                                                   shape=[num_groups, num_groups],
                                                   initializer=weights_initializer,
                                                   regularizer=l2_regularizer,
                                                   trainable=True,
                                                   restore=True)
        net = [tf.expand_dims(tf.reshape(net_split, shape=[-1]), -1) for net_split in net]
        net = tf.concat(net, axis=-1) # [D*B, G]
        net = tf.matmul(net, learnable_Adj_weights)  # [D*B, G]
        net_splits = tf.split(net, num_groups, axis=-1)
        net = [tf.reshape(net_split, shape=[n, h, w, num_outputs// num_groups]) for net_split in net_splits]
        net = tf.concat(net, axis=-1)

    return net

def _residual_group(ins, num_out=None, group_num = 14):
    ''' the bottleneck residual module
    Args:
        ins: the inputs
        k: kernel size
        num_out: number of the output feature maps, default set as the same as input
    Returns:
        residual network output
    '''
    num_in = ins.shape[-1].value
    if num_out is None:
        num_out = num_in

    with scopes.arg_scope([ops.conv2d],
                         stddev=0.01,
                         activation=tf.nn.relu,
                         batch_norm_params=_batch_norm_params,
                         weight_decay=0.0005,
                         stride=1,
                         padding='SAME'):
        #half_num_in = int(num_in//2)
        half_num_in = num_in
        #out_1 = ops.conv2d(ins, half_num_in, [1,1])
        #out_1 = standard_group_conv(group_num, ins, half_num_in, kernel_size=1, scope='sgc_1')
        out_1 = standard_group_conv_CAM(group_num, ins, half_num_in, kernel_size=1, scope='sgc_1')
        k = FLAGS.kernel_size
        #out_1 = ops.conv2d(out_1, half_num_in, [k,k])
        #out_1 = standard_group_conv(group_num, out_1, half_num_in, kernel_size=k, scope='sgc_2')
        out_1 = standard_group_conv_CAM(group_num, out_1, half_num_in, kernel_size=k, scope='sgc_2')
        #out_1 = ops.conv2d(out_1, num_out, [1,1])
        #out_1 = standard_group_conv(group_num, out_1, num_out, kernel_size=1, scope='sgc_3')
        out_1 = standard_group_conv_CAM(group_num, out_1, num_out, kernel_size=1, scope='sgc_3')

        if num_out == num_in:
            out_2 = ins
        else:
            out_2 = ops.conv2d(ins, num_out, [1,1])
        return out_1+out_2

def _residual(ins, num_out=None):
    ''' the bottleneck residual module
    Args:
        ins: the inputs
        k: kernel size
        num_out: number of the output feature maps, default set as the same as input
    Returns:
        residual network output
    '''
    num_in = ins.shape[-1].value
    if num_out is None:
        num_out = num_in

    with scopes.arg_scope([ops.conv2d],
                         stddev=0.01,
                         activation=tf.nn.relu,
                         batch_norm_params=_batch_norm_params,
                         weight_decay=0.0005,
                         stride=1,
                         padding='SAME'):
        half_num_in = int(num_in//2)
        out_1 = ops.conv2d(ins, half_num_in, [1,1])
        k = FLAGS.kernel_size
        out_1 = ops.conv2d(out_1, half_num_in, [k,k])
        out_1 = ops.conv2d(out_1, num_out, [1,1])

        if num_out == num_in:
            out_2 = ins
        else:
            out_2 = ops.conv2d(ins, num_out, [1,1])
        return out_1+out_2

MID_FEA_MAP = None
def _hourglass(ins, n, group):
    ''' hourglass is created recursively, each time the module spatial resolution remains the same
    '''
    upper1 = _residual(ins)

    k = FLAGS.kernel_size
    lower1 = ops.max_pool(ins, [k,k], stride=2, padding='SAME')
    lower1 = _residual(lower1)

    if n > 1:
        lower2 = _hourglass(lower1, n-1, group)
    else:
        lower2 = lower1

    lower3 = _residual(lower2)
    upper2 = ops.upsampling_nearest(lower3, 2)
    print('[hourglass] n={}, shape={}'.format(n, upper1.shape))

    return upper1+upper2

def detect_net(dm_inputs, cfgs, coms, num_jnt, is_training=True, scope=''):
    global CAM_num
    CAM_num = 0
    end_points = {}
    end_points['hm_outs'] = []
    end_points['hm3_outs'] = []
    end_points['um_outs'] = []

    with tf.name_scope(scope, 'hg_net'):
        with scopes.arg_scope([ops.conv2d, ops.fc, ops.batch_norm, ops.dropout],
                              is_training = is_training):
            
            input_w, input_h = dm_inputs.shape[2].value, dm_inputs.shape[1].value

            # initial image processing (from 512*512 -> 128*128)
            with tf.variable_scope('hg_imgproc'):
                # 512*512 -> 256*256
                conv_1 = ops.conv2d(dm_inputs, num_jnt*2, [7,7], stride=2, padding='SAME',
                                   batch_norm_params=_batch_norm_params, weight_decay=0.0005)
                conv_2 = _residual(conv_1, num_jnt*4)

                # 256*256 -> 128*128
                pool_1 = ops.max_pool(conv_2, kernel_size=2, stride=2, padding='SAME')
                conv_3 = _residual(pool_1)
                conv_4 = _residual(conv_3, FLAGS.num_fea)
                hg_ins = conv_4

                global MID_FEA_MAP
                MID_FEA_MAP = hg_ins

            if input_w == 512 and input_h == 512:
                num_resize = 6
            elif input_w == 256 and input_h == 256:
                num_resize = 5
            elif input_w == 128 and input_h == 128:
                num_resize = 4

            else:
                raise ValueError('unknown input depth map shape')

            output_w, output_h = int(input_w/4), int(input_h/4)
            batch_size = tf.shape(dm_inputs)[0]
            tiny_dm = tf.image.resize_images(dm_inputs, (output_h, output_w), 2)

            uu, vv = tf.meshgrid(tf.range(output_h), tf.range(output_w))
            uu, vv = tf.cast(uu, tf.float32), tf.cast(vv, tf.float32)
            uu = tf.expand_dims(tf.divide(uu, float(output_w/2)) - 1.0, axis=-1)
            vv = tf.expand_dims(tf.divide(vv, float(output_h/2)) - 1.0, axis=-1)
            uu = tf.expand_dims(uu, axis=0)
            vv = tf.expand_dims(vv, axis=0)
            uu = tf.tile(uu, [batch_size, 1, 1, 1])
            vv = tf.tile(vv, [batch_size, 1, 1, 1])
            uvd = tf.concat([uu,vv,tiny_dm], axis=-1) 

            # the hour glass
            for i in range(FLAGS.num_stack):
                hg_outs = _hourglass(hg_ins, n=num_resize, group = num_jnt)

                ll = _residual(hg_outs)
                ll = ops.conv2d(ll, FLAGS.num_fea, [1,1], stride=1, padding='SAME',
                                activation=tf.nn.relu,
                                batch_norm_params=_batch_norm_params,
                                weight_decay=0.0005)
                
                hm_out = ops.conv2d(ll, num_jnt, [1,1], stride=1, padding='SAME',
                                    activation=None,
                                    weight_decay=0.0005)
                
                hm3_in = tf.concat([ll, uvd], axis=-1)
                hm3_in = _residual(hm3_in, 128)
                hm3_out = ops.conv2d(hm3_in, num_jnt, [1,1], stride=1, padding='SAME',
                                    activation=None,
                                    weight_decay=0.0005)

                um_in = tf.concat([hg_outs, hm_out, hm3_out], axis=-1)
                um_in = _residual(_residual(um_in, 256))

                um_in_mask = tf.concat([hg_outs, hm_out, hm3_out], axis=-1)
                mask = tf.tile(tf.less(tiny_dm, -0.9), (1,1,1,um_in_mask.get_shape()[-1].value))
                um_in_mask = tf.where(mask, tf.zeros_like(um_in_mask), um_in_mask)
                um_in_mask = _residual(_residual(um_in_mask, 256))

                um_in_comb = tf.concat([um_in, um_in_mask], axis=-1)
                um_in_comb = _residual(um_in_comb)
                um_in_comb = tf.concat([um_in_comb, uvd], axis=-1)

                um_full = ops.conv2d(um_in_comb, 512, [1,1], stride=1, padding='SAME',
                                         activation=tf.nn.relu,
                                         batch_norm_params=None,
                                         weight_decay=0.0005)
                um_full = ops.dropout(um_full)
                um_full = ops.conv2d(um_full, 512, [1,1], stride=1, padding='SAME',
                                         activation=tf.nn.relu,
                                         batch_norm_params=None,
                                         weight_decay=0.0005)
                um_full = ops.dropout(um_full)

                um_out = ops.conv2d(um_full, num_jnt*3, [1,1], stride=1, padding='SAME',
                                     activation=None,
                                     batch_norm_params=None,
                                     weight_decay=0.0005)
                # with tf.variable_scope('final_GNN_hm_out_'+str(i)):
                #     hm_out_list = []
                #     hm3_out_list = []
                #     for i in range(2):
                #         hm_out = _residual_group(hm_out)
                #         hm_out_list.append(hm_out)
                #         hm3_out = _residual_group(hm3_out)
                #         hm3_out_list.append(hm3_out)
                #     hm_out = tf.concat(hm_out_list, axis=-1)
                #     hm3_out = tf.concat(hm3_out_list, axis=-1)
                #
                #     hm_out = ops.conv2d(hm_out, num_jnt, [1,1], stride=1, padding='SAME',
                #                         activation=None,
                #                         weight_decay=0.0005)
                #     hm3_out = ops.conv2d(hm3_out, num_jnt, [1,1], stride=1, padding='SAME',
                #                         activation=None,
                #                         weight_decay=0.0005)


                end_points['hm_outs'].append(hm_out)
                end_points['hm3_outs'].append(hm3_out)
                end_points['um_outs'].append(um_out)

                if i < FLAGS.num_stack-1:
                    tmp_out = tf.concat([hm_out, hm3_out, um_out], axis=-1)
                    tmp_out_reshaped = ops.conv2d(tmp_out, FLAGS.num_fea, [1,1], stride=1,
                                                 batch_norm_params=None, 
                                                 activation=None)
                    inter = ops.conv2d(ll, FLAGS.num_fea, [1,1], stride=1,
                                      batch_norm_params=None, 
                                      activation=None)

                    hg_ins = hg_ins + tmp_out_reshaped + inter

            return end_points 


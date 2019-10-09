# Copyright 2019 Bytedance Inc. or its affiliates. All Rights Reserved.
# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import warnings
import mxnet as mx

from byteps.mxnet.ops import byteps_push_pull, byteps_declare_tensor
from byteps.mxnet.ops import init, shutdown
from byteps.mxnet.ops import size, local_size, rank, local_rank

# huhanpeng
from byteps.mxnet.ops import get_comm_time
import logging
import sys, os
from mxnet import profiler
import json
import networkx as nx

parameter_index = 0

def log(s):
    if rank() == 0:
        print(s)
        sys.stdout.flush()

class Recorder(object):
    # huhanpeng: class used to collect trace info
    def __init__(self):
        self.time_dict = {"traceEvents":[]}
        self.idx_dict = {}
        self.para_name_list = None
        self.step_cnt = 0
        if os.environ.get("TRACE_ON") != 'ON':
            self._end_trace = True
            return
        self._end_trace = False
        self.end_step = int(os.environ.get("TRACE_END_STEP"))\
                    if os.environ.get("TRACE_END_STEP") \
                    else 10
        self.trace_dir = os.environ.get("TRACE_DIR") + "/" if os.environ.get("TRACE_DIR") else ""
        self.trace_path = self.trace_dir + 'bps_trace_local_rank%s_%dstep.json' % (os.environ.get("BYTEPS_LOCAL_RANK"), self.end_step)

        """config the mxnet profile"""
        profiler.set_config(profile_symbolic=True,
                    profile_imperative=False,
                    profile_memory=False,
                    profile_api=False,
                    # profile_process=False,
                    aggregate_stats=False, 
                    filename=self.trace_path)
        profiler.set_state('run')
        self.dag = nx.DiGraph()

    def add_record(self, index, _check_stop=False):
        if self._end_trace:
            return False

        if index not in self.idx_dict:
            self.idx_dict[index] = False

        if self.idx_dict[index]:
            if False not in self.idx_dict.values():
                """All parameters have been recorded, end profiling"""
                self._end_trace = True   
                self._save()
            return False # the communication traces of this parameter have been read

        """ Since each parameter will call this function, to decide when to stop profiling,
            we only focus on one parameter, e.g., the first parameter."""
        if _check_stop:
            self.step_cnt += 1
            
        if self.step_cnt >= self.end_step:
            if self.para_name_list is None:
                self.para_name_list = []
                with open(os.path.join(os.environ.get('TRACE_DIR'), 'arg_namesINpara_names.txt'), 'r') as lines:
                    for line in lines:
                        name = line[:-1]
                        self.para_name_list.append(name)
            return True
        else:
            return False            

    def end_trace(self):
        return self._end_trace

    def _save(self, add_events=None):
        """save the MXNet profiling results first"""
        profiler.set_state('stop')
        profiler.dump()
        """Note: open the file in append mode"""
        with open(self.trace_path, 'r') as f:
            mxnet_traces = json.load(f)
        # mxnet_traces={"traceEvents":[]}
        rst_traces = self.add_dependency(mxnet_traces)

        self.time_dict["traceEvents"] += rst_traces["traceEvents"]
        with open(self.trace_path, 'w') as f:
            json.dump(self.time_dict, f, indent=4)
        nx.write_gml(self.dag, self.trace_dir + "dag.gml", lambda x: str(x))
        log("Stop tracing, output trace: %s" % self.trace_path)
        """ clear the time dict after save it"""
        self.time_dict = None

    def add_dependency(self, mxnet_traces):
        index = 0
        rst_traces = {"traceEvents": []}
        while index < len(mxnet_traces["traceEvents"]):
            trace = mxnet_traces["traceEvents"][index]
            _name = trace["name"]
            # add for mxnet-gluon case
            if "name=" in _name:
                name = _name.split("name=")[1].split(";")[0]

            if trace["ph"] != 'B' and trace["ph"] != 'b':
                index += 1
                continue
            if "_backward" in name: # backward nodes
                name = name.split("_backward")[0]
                if name not in self.dag.nodes:
                    index += 1
                    continue
                # innodes = ["BW." + _n for _n in self.dag.nodes[name]["out"]]
                innodes = ["BW." + _n for _n in self.dag.successors(name)]
                name = "BW." + name
            elif name not in self.dag.nodes:
                index += 1
                continue
            else: # forward nodes
                # innodes = ["FW." + _n for _n in self.dag.nodes[name]["in"]] + self.dag.nodes[name]["var"]
                innodes = ["FW." + _n for _n, _ in self.dag.in_edges(name)] + self.dag.nodes[name]["var"]
                name = "FW." + name
            args = {"name": name}
            for i, _n in enumerate(innodes):
                args["arg%d"%i] = _n
            trace["name"] = name
            trace["args"] = args

            while True:
                index += 1
                next_trace = mxnet_traces["traceEvents"][index]
                if next_trace["ph"] == 'e' or next_trace["ph"] == 'E':
                    break
            if name.split(".")[1] not in next_trace["name"]:
                raise ValueError("'b/B' events must be followed with 'e/E' events!!!")
            trace["dur"] = next_trace['ts'] - trace['ts']
            rst_traces["traceEvents"].append(trace)
            index += 1

        return rst_traces


class DistributedOptimizer(mx.optimizer.Optimizer):
    """This is where BytePS's DistributedOptimizer wrapper for MXNet goes"""
    def __init__(self, optimizer, sym):
        self._optimizer = optimizer
        # huhanpeng: debug
        log("This is a new DistributedOptimizer with auto profiling")

        """tracing configure""" 
        self.recorder = Recorder()
        self._symbol = sym

        # para_names = self._symbol.attr_dict().keys()
        self.gen_dag(self._symbol.debug_str())

    """huhanpeng: add to construct a DAG"""
    def gen_dag(self, s):
        blocks = s.split("--------------------\n")
        index = 0
        for i in range(1, len(blocks)):
            prev_block = blocks[i-1]
            var = []
            prev_ls = prev_block.split('\n')
            for l in prev_ls:
                if "Variable" in l:
                    var.append(l.split('Variable:')[1])
            block = blocks[i]
            ls = block.split('\n')
            if 'Name' not in ls[0]:
                index += 1
                continue
            name = ls[0].split('Name=')[1]
            args = []
            for l in ls:
                if "arg[" in l:
                    arg_name = l.split(']=')[1].split('(')[0]
                    if arg_name not in var:
                        args.append(arg_name)
            for innode in args:
                self.recorder.dag.add_edges_from([(innode, name)])
            if name in self.recorder.dag.nodes:
                self.recorder.dag.nodes[name]["var"] = ["Comm." + e for e in var]
            else:
                # for the first node, it has no arg, so not be defined yet
                self.recorder.dag.add_node(name, var=["Comm." + e for e in var])           
            index += 1

    def __getattr__(self, item):
        return getattr(self._optimizer, item)

    def create_state_multi_precision(self, index, weight):
        return self._optimizer.create_state_multi_precision(index, weight)

    # huhanpeng
    def byteps_record_comm(self, index, tensor, name):
        # huhanpeng: can be removed
        if self.recorder.end_trace():
            return

        '''read communication traces offline'''
        _ts_dur_list = get_comm_time(tensor, name) 

        def return_event(index, _ts, _dur):
            if _ts == 0:
                raise ValueError("_ts should not be 0")
            para_name = self.recorder.para_name_list[index]
            op_name = "_".join(para_name.split("_")[:-1])
            return {
                    "name": "Comm." + para_name,
                    "ts": _ts,
                    "dur": _dur,
                    "ph": "X",
                    "pid": "Comm." + para_name,
                    "args": {
                        "name": "Comm." + para_name,
                        "input0": "BW." + op_name
                        }
                    }
        self.recorder.time_dict["traceEvents"] += [return_event(index, _ts, _dur) for (_ts, _dur) in _ts_dur_list]
        self.recorder.idx_dict[index] = True # avoid repeatedly read
        # log("_ts: %s, _dur: %s" % (str(_ts), str(_dur)))
        

    def _do_push_pull(self, index, grad):
        if isinstance(index, (tuple, list)):
            for i in range(len(index)):
                byteps_declare_tensor(grad[i], "gradient_" + str(index[i]))
                byteps_push_pull(grad[i], version=0, priority=-index[i],
                                 name="gradient_" + str(index[i]), is_average=True)
        else:
            byteps_declare_tensor(grad, "gradient_" + str(index))
            byteps_push_pull(grad, version=0, priority=-index,
                             name="gradient_" + str(index), is_average=True)

        # huhanpeng: modify add_record for when the index is tuple or list, 
        if isinstance(index, (tuple, list)):
            for i in range(len(index)):
                if self.recorder.add_record(index[i], (True if index[i] == 0 else False)):
                    self.byteps_record_comm(index[i], grad[i], "gradient_" + str(index[i]))       
        else:
            self.byteps_record_comm(index, grad, "gradient_" + str(index))

    def update(self, index, weight, grad, state):
        self._do_push_pull(index, grad)
        self._optimizer.update(index, weight, grad, state)

    def update_multi_precision(self, index, weight, grad, state):
        self._do_push_pull(index, grad)
        self._optimizer.update_multi_precision(index, weight, grad, state)

    def set_learning_rate(self, lr):
        self._optimizer.set_learning_rate(lr)

    def set_lr_mult(self, args_lr_mult):
        self._optimizer.set_lr_mult(args_lr_mult)

    def set_wd_mult(self, args_wd_mult):
        self._optimizer.set_wd_mult(args_wd_mult)


def broadcast_parameters(params, root_rank=0):
    """
    Broadcasts the parameters from root rank to all other processes.
    Typical usage is to broadcast the `Module.get_params()`.

    Arguments:
        params: dict of parameters to broadcast
        root_rank: The rank of the process from which parameters will be
                   broadcasted to all other processes.
    """
    global parameter_index

    if isinstance(params, dict):
        tensors = [p for _, p in sorted(params.items())]

        # Run tensor initilization
        for i in range(len(tensors)):
            byteps_declare_tensor(tensors[i], "parameter_" + str(parameter_index))
            # Broadcast is implemented as push + pull in BytePS
            # To broadcast: we should zero-out all non-root tensors, and disable push_pull average
            if rank() != root_rank:
                tensors[i].__imul__(0)
            byteps_push_pull(tensors[i], version=0, priority=0,
                             name="parameter_" + str(parameter_index), is_average=False)
            parameter_index += 1

        # Make sure tensors pushed to MXNet engine get processed such that all
        # workers are synced before starting training.
        for tensor in tensors:
            tensor.wait_to_read()

    elif isinstance(params, mx.gluon.parameter.ParameterDict):
        raise TypeError("For gluon users, you should not call this function. "
                        "DistributedTrainer will broadcast all parameters at "
                        "the first training step.")

    else:
        raise ValueError('Invalid params of type: %s' % type(params))


class DistributedTrainer(mx.gluon.Trainer):
    """A subclass of MXNet gluon.Trainer.

    There are two differences between DistributedTrainer and Trainer:
    1. DistributedTrainer calculates gradients using BytePS push pull
       API while Trainer does it using kvstore push/pull APIs;
    2. DistributedTrainer performs push_pull(summation) and average,
       while Trainer only performs push_pull(summation).

    Parameters
    ----------
    params : ParameterDict
        The set of parameters to optimize.
    optimizer : str or Optimizer
        The optimizer to use. See
        `help <http://mxnet.io/api/python/optimization/optimization.html#the-mxnet-optimizer-package>`_
        on Optimizer for a list of available optimizers.
    optimizer_params : dict
        Key-word arguments to be passed to optimizer constructor. For example,
        `{'learning_rate': 0.1}`. All optimizers accept learning_rate, wd (weight decay),
        clip_gradient, and lr_scheduler. See each optimizer's
        constructor for a list of additional supported arguments.
    """

    def __init__(self, params, optimizer, optimizer_params=None, root_rank=0):
        if isinstance(optimizer, DistributedOptimizer):
            optimizer = optimizer._optimizer
            warnings.warn("DistributedTrainer does not take DistributedOptimizer "
                          "as its optimizer. We have unwrapped it for you.")

        super(DistributedTrainer, self).__init__(
            params, optimizer, optimizer_params=optimizer_params, kvstore=None)

        # _scale is used to check and set rescale_grad for optimizer in Trainer.step()
        # function. Normalizing it by BytePS size, which is equivalent to performing
        # average in push_pull, has better performance.
        self._scale /= size()
        self.root_rank = root_rank

    def _allreduce_grads(self):
        for i, param in enumerate(self._params):
            if param.grad_req != 'null':
                byteps_declare_tensor(param.list_grad()[0], "gradient_" + str(i))
                byteps_push_pull(param.list_grad()[0], is_average=False,
                                 name="gradient_" + str(i), priority=-i)
                # huhanpeng
                self.byteps_record_comm(param.list_grad()[0], "gradient_" + str(i))

    def _init_params(self):
        tensors = []
        for param in self._params_to_init:
            if param._deferred_init:
                tensors.append(param)
            else:
                param_arrays = param._check_and_get(param._data, list)
                idx = self._param2idx[param.name]
                byteps_declare_tensor(param_arrays[0], "parameter_" + str(idx))

                if rank() != self.root_rank:
                    param_arrays[0].__imul__(0)
                byteps_push_pull(param_arrays[0], version=0, priority=0,
                                 name="parameter_" + str(idx), is_average=False)
                param_arrays[0].wait_to_read()

        self._params_to_init = tensors

    # huhanpeng
    def byteps_record_comm(self, tensor, name):
        _ts, _dur = get_comm_time(tensor, name)
        log("_ts: %s, _dur: %s" % (str(_ts), str(_dur)))
        # \TODO: how to get the name



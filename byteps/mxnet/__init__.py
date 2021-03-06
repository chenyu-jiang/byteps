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
import os

from byteps.mxnet.ops import byteps_push_pull, byteps_declare_tensor
from byteps.mxnet.ops import init, shutdown
from byteps.mxnet.ops import size, local_size, rank, local_rank

# append for auto_profiling
import logging
import sys, os
from mxnet import profiler
import json
import networkx as nx
import threading
import time

parameter_index = 0

QueueType = [
  "COORDINATE_REDUCE",
  "REDUCE",
  "COPYD2H",
  "PCIE_REDUCE",
  "COORDINATE_PUSH",
  "PUSH",
  "PULL",
  "COPYH2D",
  "COORDINATE_BROADCAST",
  "BROADCAST",
  "QUEUE_NUM_AND_NOT_A_REAL_QUEUE_TYPE_AND_MUST_BE_THE_LAST"
]

def BYTEPS_TRACE_DEBUG(s, debug=False):
    #! log debug info when debug is True and env HHP_DEBUG is set
    if rank() == 0 and ((debug and os.getenv("HHP_DEBUG", None)) or not debug) :
        print(s)
        sys.stdout.flush()


class Recorder(object):
    #! class used to collect trace info
    def __init__(self, profile_symbolic=True,
                    profile_imperative=False,
                    profile_memory=False,
                    profile_api=False,
                    aggregate_stats=False):
        self.time_dict = {"traceEvents":[]}
        self.idx_dict = {}
        self.gradient_name_list = None
        self.step_cnt = 0
        if os.environ.get("BYTEPS_TRACE_ON", "") != '1':
            self._end_trace = True
            return
        self._end_trace = False
        self.end_step = int(os.environ.get("BYTEPS_TRACE_END_STEP", "30"))
        self.start_step = int(os.environ.get("BYTEPS_TRACE_START_STEP", "20"))
        self.trace_dir = os.environ.get("BYTEPS_TRACE_DIR", ".") + "/" + os.environ.get("BYTEPS_LOCAL_RANK") + "/"
        if not os.path.exists(self.trace_dir):
            os.makedirs(self.trace_dir)
        else:
            if os.path.exists(self.trace_dir + "comm.json"):
                os.remove(self.trace_dir + "comm.json")
            if os.path.exists(self.trace_dir + "io.json"):
                os.remove(self.trace_dir + "io.json")
        self.trace_path = self.trace_dir + 'bps_trace_local_rank%s_%dstep.json' % (os.environ.get("BYTEPS_LOCAL_RANK"), self.end_step)

        """config the mxnet profile"""

        profiler.set_config(profile_symbolic=profile_symbolic,
                    profile_imperative=profile_imperative,
                    profile_memory=profile_memory,
                    profile_api=profile_api,
                    # profile_process=False,
                    aggregate_stats=aggregate_stats, 
                    filename=self.trace_dir+'temp.json')

        if not self._end_trace and self.start_step < 1:
            raise ValueError("BYTEPS_TRACE_START_STEP must be larger than 1")
        if not self._end_trace and self.end_step <= self.start_step:
            raise ValueError("BYTEPS_TRACE_END_STEP must be larger than BYTEPS_TRACE_START_STEP")
        if self.step_cnt == self.start_step - 1:
            profiler.set_state('run')

        self.dag = None
        self.loss_dag = []

        #! symbol/block, used to get the dependency info, at least one should be given
        self.block = None
        self.symbol = None
        self.loss = None

    def scheduler(self, index, _check_stop=False):
        '''A scheduler, manage the counter for each gradient, `self.idx_dict` is 
        used to record the status of each gradient, the fist time a gradinet call 
        this function, register the `index` to self.idx_dict with False; when it
        becomes True, this gradinet is ready to output traces (the communication 
        traces of this gradient have been collected); Output traces only when 
        the status of gradients are True.

        Parameters:
        ----------
        index : int
            The index of the gradient.
        _check_stop : bool
            if the flag is set, add the step_cnt by 1.

        Returns:
        ----------
        bool, whether to collect the communication trace of this gradinet.
        '''
        if self._end_trace:
            return False
        if index not in self.idx_dict:
            self.idx_dict[index] = False
            
        def get_traces(self):
            #! Sleep to wait for all the communication traces have been printed.
            time.sleep(5) 
            self.save_trace()

        if self.idx_dict[index]:
            if False not in self.idx_dict.values():
                """All parameters have been recorded, end profiling"""
                self._end_trace = True 
                #! Inform IO recorder to stop profiling
                os.environ["BYTEPS_TRACE_STATUS"] = "END"
                #! Output mxnet traces and import it
                profiler.set_state('stop')
                profiler.dump()
                #! Create a new thread to process traces
                _t = threading.Thread(target=get_traces, args=(self,))
                _t.start()            
            return False # the communication traces of this parameter have been read

        """ Since each parameter will call this function, to decide when to stop profiling,
            we only focus on one parameter, e.g., the first parameter.
        """
        if _check_stop:
            self.step_cnt += 1

        if self.step_cnt == self.start_step - 1:
            profiler.set_state('run')
            
        if self.step_cnt >= self.end_step:
            if self.gradient_name_list is None:
                self.gradient_name_list = []
                with open(os.path.join(self.trace_dir, 'arg_namesINpara_names.txt'), 'r') as lines:
                    for line in lines:
                        name = line[:-1]
                        self.gradient_name_list.append(name)
            return True
        else:
            return False            

    def end_trace(self):
        return self._end_trace

    def wait_for_trace(self, ready, name):
        WAIT_TIME_OUT = 10
        WAIT_TIME = 0.1
        wait_cnt = 0.0
        while True:
            if ready():
                break
            time.sleep(WAIT_TIME)
            wait_cnt += 1
            if wait_cnt * WAIT_TIME > WAIT_TIME_OUT:
                print(os.environ.get("BYTEPS_TRACE_COMM_READY", "-1"))
                raise ValueError("Waiting Time Out, wait %s traces for %f s" % (name, wait_cnt * WAIT_TIME))
        BYTEPS_TRACE_DEBUG("Wait %s traces for %f s" % (name, wait_cnt * WAIT_TIME))

    def byteps_collect_io(self):
        def io_ready():
            return os.path.exists(os.path.join(self.trace_dir, "io.json"))
        self.wait_for_trace(io_ready, "I/O")
        with open(os.path.join(self.trace_dir, "io.json"), 'r') as f:
            rst_traces = json.load(f)
        self.time_dict["traceEvents"] += rst_traces["traceEvents"]

    def byteps_collect_comm(self):
        #! read communication traces offline
        def comm_ready():
            return os.path.exists(os.path.join(self.trace_dir, "comm.json"))
        self.wait_for_trace(comm_ready, "Comm")
        with open(os.path.join(self.trace_dir, "comm.json"), 'r') as f:
            rst_traces = json.load(f)
        for trace in rst_traces["traceEvents"]:
            if "byteps.gradient_" not in trace["args"]["name"]:
                continue
            para_index = int(trace["args"]["name"].split("_")[-1])
            para_name = self.gradient_name_list[para_index]
            if trace["name"] != trace["args"]["name"]:
                #! subtask
                trace["name"] = "Comm." + para_name + "." + trace["name"].split(".")[-1]
            else:
                #! main task
                trace["name"] = "Comm." + para_name
            trace["pid"] = "Comm." + para_name
            trace["args"]["name"] = "Comm." + para_name
            input_nodes = [u for u, _ in self.dag.in_edges("Comm." + para_name)]
            assert len(input_nodes) == 1
            trace["args"]["input0"] = list(input_nodes)[0]
            self.time_dict["traceEvents"].append(trace)

        with open(os.path.join(self.trace_dir, "gradient_name_list.txt"), "w") as f:
            for s in self.gradient_name_list:
                f.write(str(s) + "\n")

    def save_trace(self):
        ''' Output trace resutls '''
        with open(self.trace_dir + 'temp.json', 'r') as f:
            mxnet_traces = json.load(f)

        #! Get the dependency graph, adapt to DistributedOptimizer and DistributedTrainer
        if self.symbol is not None:
            self.dag = self.gen_dag(self.symbol.debug_str(), _main=True)      
        elif self.block is not None:
            symbol = self.block._cached_graph[1]
            self.dag = self.gen_dag(symbol.debug_str(), _main=True)
            self.loss_dag = [(self.gen_dag(l._cached_graph[1].debug_str(), _str_name="loss%d"%i) if l is not None else None) for i, l in enumerate(self.loss)]
            self.combine_loss_dag()
        else:
            raise ValueError("A symbol or model/block must be given when defining DistributedOptimizer/DistributedTrainer.")

        #! Apply dependencies in self.dag to the mxnet traces.
        rst_traces = self.byteps_collect_computation(mxnet_traces)

        #! Collect communication traces, IO traces and STEP traces and apply dependency
        self.byteps_collect_io()
        self.byteps_collect_comm() 

        #! Combine two kinds of trace and output them
        self.time_dict["traceEvents"] += rst_traces["traceEvents"]
        with open(self.trace_path, 'w') as f:
            json.dump(self.time_dict, f, indent=4)

        #! Output the dag, only containing forward info
        nx.write_gml(self.dag, self.trace_dir + "dag.gml", lambda x: str(x))
        BYTEPS_TRACE_DEBUG("Stop tracing, output trace: %s" % self.trace_path)
        #! clear the time dict after save it
        self.time_dict = None

    def byteps_collect_computation(self, mxnet_traces):
        '''Apply dependency info to the mxnet trace results

        Parameters
        ----------
        mxnet_traces : dict
            A dict containing MXNet trace results.

        Returns
        ----------
        rst_traces : dict
            A dict containing MXNet trace results combined with dependency info.
        '''
        
        pid = None
        rst_traces = {"traceEvents": []}

        index = 0
        traces = []
        while index < len(mxnet_traces["traceEvents"]):
            if "ts" not in mxnet_traces["traceEvents"][index]:
                index += 1
                continue
            trace = mxnet_traces["traceEvents"][index]
            if trace["ph"] == 'B' or trace["ph"] == 'b':
                next_trace = mxnet_traces["traceEvents"][index+1]
                assert trace["name"] == next_trace["name"]
                trace["dur"] = next_trace['ts'] - trace['ts']
                trace["ph"] = "X"
                traces.append(trace)
                index += 2
            else:
                index += 1

        traces = sorted(traces, key=lambda x: x["ts"], reverse=False)

        def _preprocess(_name):
            '''Fetch and handle the trace name'''
            #! add for mxnet-gluon case
            if "name=" in _name:
                _name = _name.split("name=")[1].split(";")[0]
            #! backward nodes or forward nodes
            _name = "BW." + _name.split("_backward")[0] if "_backward" in _name else "FW." + _name
            _name = _name.split("_fwd")[0] if "_fwd" in _name else _name
            return _name 

        IGNORE_OP = ["DeleteVariable", "sum", "_plus_scalar", 
                "_copyto_GPU2GPU", "broadcast_add", 
                "Reshape", "Cast", "_arange", "elemwise_add",
                "_ones", "SyncCopyGPU2CPU", "_mul_scalar"]

        def real_last_bw_name():
            statue = "init"
            _index = 0
            tmp = None
            while _index < len(traces):
                trace = traces[_index]
                _index += 1
                name = _preprocess(trace["name"])
                if name not in self.dag.nodes:
                    continue
                if statue == "init" and "FW" in name:
                    statue = "fw"
                elif statue == "fw" and "BW" in name:
                    statue = "bw"
                    tmp = name
                elif statue == "bw" and "BW" in name:
                    tmp = name
                elif statue == "bw" and "FW" in name:
                    statue = "fw"
                    return tmp
        _real_last_bw_name = real_last_bw_name()

        index = 0
        while index < len(traces):
            trace = traces[index]
            index += 1
            name = _preprocess(trace["name"])       

            if name not in self.dag.nodes:
                #! Only collect nodes in the dag
                #! TODO: some trvial nodes may also be useful
                continue

            #! deduplication
            #! TODO: should be careful, only choose one prosess here
            if pid is None:
                pid = trace["pid"]
            elif pid != trace["pid"]:
                continue

            innodes = [_n for _n, _ in self.dag.in_edges(name)]
            args = {"name": name}
            for i, _n in enumerate(innodes):
                args["input%d"%i] = _n
            trace["name"] = name
            trace["args"] = args
            rst_traces["traceEvents"].append(trace)

            #! if all STEP-dependent BW nodes have arrived, process traces til FW
            # if len(last_bw_nodes) == 0:
            if name == _real_last_bw_name:
                _step_ts = None
                _step_dur = 0
                while index < len(traces):
                    _trace = traces[index]
                    if pid != _trace["pid"]:
                        index += 1
                    else:
                        name = _preprocess(_trace["name"])
                        if name in self.dag.nodes:
                            break
                        index += 1
                        if _trace["name"] in IGNORE_OP or "operator" != _trace["cat"]:
                            pass
                        else:
                            if _step_ts is None:
                                _step_ts = _trace["ts"]
                            _step_dur = _trace["ts"] + _trace["dur"] - _step_ts
                if _step_ts is not None:
                    rst_traces["traceEvents"].append({
                        "name": "STEP",
                        "ts": _step_ts,
                        "dur": _step_dur,
                        "ph": "X",
                        "cat": "operator",
                        "pid": pid,
                        "args": {
                            "name":"STEP"
                        }
                    })

        return rst_traces

    def gen_dag(self, s, _str_name="symbol_debug_str", _main=False):
        """Construct a DAG from the mxnet info

        Parameters:
        ----------
        s : str
            Must follow the standard chrome trace format and not None.
        """
        with open(self.trace_dir + _str_name + ".txt", "w") as f:
            f.write(s)
        _dag = nx.DiGraph()
        blocks = s.split("--------------------\n")
        
        #! 3. FW -> OUTPUT and 4. OUTPUT -> BW
        first_ls = blocks[0].split('\n')
        output_cnt = 0
        for i in range(len(first_ls)):
            if "Variable:" in first_ls[i]:
                break
            if "output[" in first_ls[i]:
                output_node = first_ls[i].split(']=')[1].split('(')[0]
                output_node = output_node.split("_fwd")[0] if "_fwd" in output_node else output_node
                _dag.add_edge("FW." + output_node, "OUTPUT%d"%output_cnt)
                _dag.add_edge("OUTPUT%d"%output_cnt, "BW." + output_node)
                output_cnt += 1

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
                continue
            name = ls[0].split('Name=')[1]
            op = ls[0].split(',')[0].split("Op:")[1]
            args = []
            for l in ls:
                if "arg[" in l:
                    arg_name = l.split(']=')[1].split('(')[0]
                    if arg_name not in var:
                        args.append(arg_name)
            if "_fwd" in name:
                name = name.split("_fwd")[0]

            #! --------- construct the graph ----
            _dag.add_node("FW." + name, op=op)
            _dag.add_node("BW." + name, op=op)
            for innode in args:
                innode = innode.split("_fwd")[0] if "_fwd" in innode else innode
                #! 2. FW -> FW and 5. BW -> BW
                _dag.add_edge("FW." + innode, "FW." + name)
                _dag.add_edge("BW." + name, "BW." + innode)
            for _var in var:
                if "data" in _var:
                    _dag.add_edge("I/O", "FW." + name)
                    if _main:
                        #! 1. IO -> FW, 8. BW -> STEP -> FW                  
                        _dag.add_edge("BW." + name, "STEP")
                        _dag.add_edge("STEP", "FW." + name)
                else:
                    #! 7. Comm -> FW and 6. BW -> Comm
                    _dag.add_edge("Comm." + _var, "STEP")
                    _dag.add_edge("BW." + name, "Comm." + _var)
        return _dag

    def combine_loss_dag(self):
        for idx, ld in enumerate(self.loss_dag):
            if ld is None:
                continue
            output_name = "OUTPUT%d"%idx
            output_node = [u for u, _ in self.dag.in_edges(output_name)][0]
            first_bw_node = list(self.dag.successors(output_name))[0]
            for u, v in ld.edges():
                if "I/O" in u:
                    self.dag.add_edge(output_node, v)
                    self.dag.add_edge("BW." + v.split("FW.")[1], first_bw_node)
                elif "OUTPUT" in u:
                    self.dag.add_edge(output_name, v)
                elif "OUTPUT" in v:
                    self.dag.add_edge(u, output_name)
                else: 
                    self.dag.add_edge(u, v)

        self.loss_dag = None


    def end4index(self, index, tensor, name):
        ''' Offline collect the communication trace results of gradient `index`

        Parameters
        ----------
        index : int
            The index of the gradient.
        tensor: tensor
            A tensor to average and sum.
        name : str
            A name of the reduction operation.
        '''
        if self.end_trace():
            return
        self.idx_dict[index] = True # avoid repeatedly read

class DistributedOptimizer(mx.optimizer.Optimizer):
    """This is where BytePS's DistributedOptimizer wrapper for MXNet goes"""
    def __init__(self, optimizer, sym=None):
        self._optimizer = optimizer
        BYTEPS_TRACE_DEBUG("This is a new DistributedOptimizer with auto profiling")
        """tracing configure""" 
        self.recorder = Recorder()
        self.recorder.symbol = sym

        self._enable_async = (int(os.getenv('BYTEPS_ENABLE_ASYNC', 0)) != 0)
        if self._enable_async:
            assert int(os.getenv('DMLC_NUM_WORKER'))>1, \
                "Async is only valid for distributed training"
            print('BytePS: enable asynchronous training')

    def __getattr__(self, item):
        return getattr(self._optimizer, item)

    def create_state_multi_precision(self, index, weight):
        return self._optimizer.create_state_multi_precision(index, weight)

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
        # modify scheduler for when the index is tuple or list, 
        if isinstance(index, (tuple, list)):
            for i in range(len(index)):
                if self.recorder.scheduler(index[i], (True if index[i] == 0 else False)):
                    self.recorder.end4index(index[i], grad[i], "gradient_" + str(index[i]))       
        else:
            if self.recorder.scheduler(index, (True if index == 0 else False)):
                self.recorder.end4index(index, grad, "gradient_" + str(index))


    def _do_push_pull_param(self, index, delta_weight):
        # not implemented
        raise ValueError("Not implemented")

        if isinstance(index, (tuple, list)):
            for i in range(len(index)):
                byteps_declare_tensor(delta_weight[i], "weight_" + str(index[i]))
                byteps_push_pull(delta_weight[i], version=0, priority=-index[i],
                                 name="weight_" + str(index[i]), is_average=False)
        else:
            byteps_declare_tensor(delta_weight, "weight_" + str(index))
            byteps_push_pull(delta_weight, version=0, priority=-index,
                             name="weight_" + str(index), is_average=False)

    def update(self, index, weight, grad, state):
        if self._enable_async:
            temp_weight = weight.copy()
            self._optimizer.update(index, weight, grad, state)
            # push delta weight, and pull weight back to the same tensor
            weight.__isub__(temp_weight)
            self._do_push_pull_param(index, weight)
        else:
            self._do_push_pull(index, grad)
            self._optimizer.update(index, weight, grad, state)

    def update_multi_precision(self, index, weight, grad, state):
        if self._enable_async:
            temp_weight = weight.copy()
            self._optimizer.update_multi_precision(index, weight, grad, state)
            # push delta weight, and pull weight back to the same tensor
            weight.__isub__(temp_weight)
            self._do_push_pull_param(index, weight)
        else:
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

    def __init__(self, params, optimizer, 
                optimizer_params=None, 
                root_rank=0, 
                block=None,
                **kwargs):
        if isinstance(optimizer, DistributedOptimizer):
            optimizer = optimizer._optimizer
            warnings.warn("DistributedTrainer does not take DistributedOptimizer "
                          "as its optimizer. We have unwrapped it for you.")

        BYTEPS_TRACE_DEBUG("This is a new DistributedTrainer with auto profiling")
        self.recorder = Recorder(profile_symbolic=True,
                    profile_imperative=True,
                    profile_memory=False,
                    profile_api=False,
                    aggregate_stats=False)
        # self.recorder.gradient_name_list = [param.name for param in list(params.values)]
        self.recorder.gradient_name_list = [gradient_name for gradient_name in list(params)]
        if block is None:
            raise ValueError("`block` must be given to define DistributedTrainer")
        self.recorder.block = block
        self.recorder.loss = kwargs["loss"] if "loss" in kwargs else None
        self.imported_net = None

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
            # check whether to collect traces
            if self.recorder.scheduler(i, (True if i == 0 else False)) and param.grad_req != 'null':
                self.recorder.end4index(i, param.list_grad()[0], "gradient_" + str(i))

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

#!/bin/bash

PY_VERSION="3"
path="`dirname $0`"


if [ "$PY_VERSION" = "3" ]; then
	PYTHON="python3"
elif [ "$PY_VERSION" = "2" ]; then	
	PYTHON="python"
else
	echo "Python version error"
fi

BYTEPS_PATH=`${PYTHON} -c "import byteps as bps; path=str(bps.__path__); print(path.split(\"'\")[1])"`
echo "BYTEPS_PATH:${BYTEPS_PATH}" 
# BYTEPS_PATH: /usr/local/lib/python3.6/site-packages/byteps-0.1.0-py3.6-linux-x86_64.egg/byteps/torch

##----------------------------------- 		Modify MXNet 	  ----------------------------------- 
# \TODO huhanpeng: direct get the gradient names in bytePS without modifying MXNet python part
if [ $DMLC_ROLE = "worker" ]; then
	echo "Modify MXNet for workers"
	MX_PATH=`${PYTHON} -c "import mxnet; path=str(mxnet.__path__); print(path.split(\"'\")[1])"`
	echo "MX_PATH: $MX_PATH"
	${PYTHON} $path/insert_code.py \
			--target_file="$MX_PATH/module/executor_group.py" \
			--start="        self.arg_names = symbol.list_arguments()" \
			--end="        self.aux_names = symbol.list_auxiliary_states()" \
			--indent_level=2 \
			--content_str="import os
_param_names = [name for i, name in enumerate(self.arg_names) if name in self.param_names]
path = os.environ.get('TRACE_DIR')
if path:
	with open(os.path.join(path, 'arg_namesINpara_names.txt'), 'w') as f:
		for name in _param_names:
			f.write('%s\n' % name) # output execution graph"
else
	echo "No need to modify mxnet for server/scheduler."
fi

## To avoid integrating multiple operators into one single events
# \TODO: may influence the performance
export MXNET_EXEC_BULK_EXEC_TRAIN=0

## install networkx
pip3 install networkx

##----------------------------------- Start to run the program ----------------------------------- 
echo 
echo "-------------------- Start to run the program ---------------"
python $path/launch.py ${PYTHON} $path/train_imagenet_byteps.py --benchmark 1 --batch-size=32 
# --num-iters 1000


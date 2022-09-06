from examples.instrument import run_example_local

run_example_local('examples.development', '--algorithm', 'SAC_G', '--universe', 'gym', '--domain', 'HalfCheetah', '--task', 'v3', 
                  '--exp-name', 'my-sac-experiment-1', '--checkpoint-frequency', '1000', '--gpus', '1', '--trial-gpus', '1')
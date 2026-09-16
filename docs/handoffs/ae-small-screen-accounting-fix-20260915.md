# DeepSeek：筛选失败后的发送计数修正（仅记录层）

现场 transfers/ae-small-screen-live-20260915-r1/：4B第一条请求发送后约181秒发生 upstream_io_failure_no_retry；9B未发送，整批停止。journal有1条upstream_request、0条upstream_response，但result记录requests_sent_total=0、model_called=false。

原因：_run_model只在proxy.dispatch返回后累加attempt，异常路径漏记已经尝试发送的请求。只修 scripts/ae_01_small_model_screen.py 的证据计数：按journal实际upstream_request归属核发送尝试，区分发送尝试/收到响应/服务商是否执行未知；不要把本地发送前拒绝算成发送。不改重试、超时、节流、任务、模型或通过标准。

新增一个离线真实opener抛超时的定向用例：1次发送尝试、0响应、9B未启动、无重试；另核规范化前拒绝为0发送。独立交付，不覆盖现场raw，不跑全仓、不联网不部署。该问题不导致本次网络失败，只影响结果描述。

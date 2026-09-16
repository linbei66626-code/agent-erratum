# 给 DeepSeek：部署前仅适配测试源码路径

项目：`/Users/lrc/Documents/研究生资料/实验/agent-erratum`。先读AGENTS.md。R/E r3的生产逻辑和54项本地测试已验收，本任务不修改生产代码或重做研究实现。

只改 `tests/test_ae_cloud_re_pair.py` 中 `test_stop_markers_match_the_pinned_vita_constants` 的源码路径选择（当前约897行）：使用已有 `AE_VITA_SOURCE` 环境变量，未设置时保留当前Mac默认源码目录，然后拼 `src/vita/user/base.py`。显式设置路径但文件缺失时须明确失败，不能skip；保留实际AST读取固定Vita三个常量并与checker比较的断言。不用复制常量、mock期待值或创建假的/tmp路径替代真实源码读取。没有显式环境变量时的历史默认缺源skip行为可以保留。

交付到新 `results/ae-cloud-re-test-portability-20260912-r1/`：改前测试原字节、最小patch、新SHA、实际命令与日志。其余文件及r1/r2/r3/核验证据不动。

验证：显式 `AE_VITA_SOURCE` 指向真实固定源码，禁网跑现有54项无skip；另用一个确定缺失的显式路径单独运行该用例，确认有界失败且非skip。无需重复447项全量。不得SSH/部署、启动服务或调用模型；此为测试环境可迁移性适配，不改变已验生产逻辑。

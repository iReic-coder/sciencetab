# ScienceTab

把 MATLAB 科研实验变成可追溯、可对比、可复现的运行记录。本地运行，不需要云端，不调用大模型。

## 为什么

脚本型科研通常这样走到死胡同：实验入口散在多个目录、依赖靠相对路径、结果文件时间戳分不清哪次是哪次、同名配置改了参数还在读旧缓存、报告里的数值回头找不到来源。
ScienceTab 只做三件事：**登记入口、隔离运行、留痕可查**。

## 能做什么

- **RunnerSpec 登记一个实验入口**：入口命令、工作目录、只读依赖、期望产物、验证判据、超时。
- **隔离副本运行**：把登记过的依赖按原相对布局复制到 `data/runs/<run_id>/work/`，原工程只读；入口目录的 `results/` 建空目录，避免历史产物混入。
- **确定性执行**：argv 数组启动 `matlab -batch`，GBK 解码输出，真实退出码，超时与取消按 Windows 进程树处理；`cancelled` 由平台令牌判定，不看退出码（被终止的 MATLAB 同样返回 1）。
- **可核对的记录**：每次运行留下 `request/spec/manifest/completion.json`、原始日志与产物副本及 SHA-256；`manifest` 记录环境、MATLAB 版本与全部被复制依赖文件的哈希。
- **页面**：运行列表、入口详情、运行详情（配置/依赖/日志/产物/验证）、两次运行对比、来源导出（JSON）。
- **幂等**：同一 `--idempotency-key` 重复提交只产生一次运行。
- **状态**：`succeeded / failed / incomplete / validation_failed / timed_out / cancelled / interrupted`，进程消失但缺完成记录的运行会被对账标为 `interrupted`。

## 快速开始

要求：Windows + MATLAB（可用环境变量 `SCIENCETAB_MATLAB` 覆盖路径，默认 `D:\Matlab\bin\matlab.exe`）、Python 3.8+；只用标准库。

```powershell
$env:PYTHONPATH = "backend"
$py = "python"   # 换成你的 python.exe

# 1) 从示例复制一份 RunnerSpec，指向自己的科研工程
copy runspecs\example_rate_power.json runspecs\my_exp.json
# 编辑：entry.command / entry.workdir / sources.include / expected_outputs

# 2) 校验依赖（缺依赖在这里失败，而不是跑到一半）
& $py -m sciencetab check-spec --spec my_exp

# 3) 运行并查看记录
& $py -m sciencetab run --spec my_exp --idempotency-key trial-1
& $py -m sciencetab list
& $py -m sciencetab verify <run_id>
& $py -m sciencetab compare <run_a> <run_b>

# 4) 打开界面
& $py -m sciencetab serve --port 8765   # http://127.0.0.1:8765/
```

`sources.root` 支持 `${VAR}` 环境变量；相对路径按仓库根解析。

## 目录

```
backend/sciencetab/spec.py       RunnerSpec 读取、校验、环境变量展开
backend/sciencetab/workspace.py  隔离副本构建与逐文件哈希
backend/sciencetab/matlab.py     MATLAB 适配器（argv、GBK、超时、取消）
backend/sciencetab/storage.py    SQLite 索引（runs / artifacts / validations）
backend/sciencetab/run.py        运行编排、manifest/completion、幂等、对比
backend/sciencetab/server.py     本地 HTTP 服务与页面（服务端渲染，无 Node）
runspecs/                        登记的入口（示例与自检）
tests/                           验收与自检脚本
data/                            运行数据（不纳入版本控制）
```

**权威证据是 `data/runs/<run_id>/` 下的文件**，SQLite 只是查询索引；索引可疑时 `python -m sciencetab reindex` 从文件重建。

## 设计取舍

- 平台负责身份、状态、归档这类确定性工作；解释与草稿交给 Agent，两者不混。
- 不解析 MATLAB 内部状态，只依赖进程退出码、日志与产物；同一入口换成 Python 脚本时 RunnerSpec 结构不变（`entry.kind` 目前只实现 `matlab`）。
- 页面用标准库服务端渲染，不引入 Node 构建链；单机规模不需要前端框架。
- 不合并不同实验条件的运行：对比只对同规格的可比产物做哈希比较，图件等含时间戳的产物单列为非可比。

## 已知边界

- 单机单用户，不做任务队列：同时提交两次运行会并发启动两个 MATLAB。
- 不导入历史材料、不做时间线、不记录结论修订（下一阶段）。
- 未覆盖大文件产物、MAT 解析、断线重连、备份恢复。
- `entry.kind` 目前只有 `matlab`。

## 许可

MIT，见 [LICENSE](LICENSE)。
# ScienceTab 初版工作台

本地实验工作台：登记实验入口 → 在隔离副本中运行 MATLAB → 浏览器查看运行、产物、验证与来源。
不修改原科研工程，不调用模型，不引入 Node/React 或第三方 Python 依赖。

## 启动

```powershell
$env:PYTHONPATH = "D:\ScienceTab\backend"
& "C:\Program Files\python\python.exe" -m sciencetab serve --port 8765
```

打开 <http://127.0.0.1:8765/>。只绑定本机回环地址；关闭页面不影响正在运行的计算。

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 运行列表（2.5 秒自动刷新、按 id/入口/状态过滤）+ 登记入口（每个入口一个"运行"按钮） |
| `/specs/<id>` | 入口详情：依赖、期望产物、该入口的全部运行 |
| `/runs/<id>` | 运行详情：状态/耗时/退出码自动更新；产物、验证、日志、来源四个标签页；日志增量流式显示 |
| `/compare?a=&b=` | 两次运行对比：下拉选择、自动渲染、不同产物给出两侧哈希 |
| `/runs/<id>/export?format=json` | 来源清单（配置、依赖哈希、产物、验证、环境） |
| `/api/runs`、`/api/runs/<id>`、`/api/runs/<id>/log?offset=`、`/api/compare`、`/api/start`、`/api/cancel` | JSON 接口 |

交互全部走 `fetch`：启动、取消、筛选、对比都不刷新整页。POST 需要令牌（页面 `meta[name=csrf-token]`，
脚本用 `X-CSRF-Token` 头，表单用 hidden 字段），无令牌返回 403。

单槽保护：已有运行在跑时 `/api/start` 返回 409 并带上正在运行的 run_id。

## 命令行

```powershell
$env:PYTHONPATH = "D:\ScienceTab\backend"
& $py -m sciencetab check-spec --spec rr_thz_rate_power   # 校验入口与依赖
& $py -m sciencetab run --spec rr_thz_rate_power --idempotency-key demo-1
& $py -m sciencetab list
& $py -m sciencetab show <run_id>
& $py -m sciencetab verify <run_id>                       # 重算产物哈希
& $py -m sciencetab compare <run_a> <run_b>
& $py -m sciencetab cancel <run_id>
& $py -m sciencetab reindex                               # 按 run 目录重建索引
& $py -m sciencetab serve --port 8765
```

## 结构与约定

```
backend/sciencetab/spec.py       RunnerSpec 读取与校验
backend/sciencetab/workspace.py  隔离副本构建与逐文件哈希
backend/sciencetab/matlab.py     MATLAB 适配器（argv、GBK、CREATE_NO_WINDOW、超时、取消）
backend/sciencetab/storage.py    SQLite 索引：runs / artifacts / validations
backend/sciencetab/run.py        运行编排、manifest/completion、幂等、对比、校验
backend/sciencetab/server.py     本地 HTTP 服务与页面
runspecs/                        登记的实验入口
tests/                           验收与自检
data/                            metadata.sqlite、runs/<run_id>/、logs/
```

**权威证据是 run 目录里的文件**（`request/spec/manifest/completion.json` 与 `outputs/`），
SQLite 只是查询索引；索引可疑时用 `reindex` 从文件重建。

## 状态

`succeeded` / `failed`（退出码非 0）/ `incomplete`（退出 0 但缺产物）/
`validation_failed`（验证记录未全通过）/ `timed_out` / `cancelled`（平台令牌判定，不看退出码）/
`interrupted`（进程消失且缺少完成记录，启动或列表时对账）。

## 已知边界

- 单槽执行：已有运行时新的提交被拒绝（409），不做排队。
- 不导入历史材料、不做时间线、不记录结论修订（这些属于下一阶段）。
- 对比只对同规格运行有意义，跨条件不合并，也不判断科学结论。

## 自检

```powershell
$env:PYTHONPATH = "backend"
& $py tests/test_state_machine.py       # 六种终态
& $py tests/test_reconcile.py           # 中断对账
& $py tests/test_idempotency_stability.py
& $py tests/test_web_ui.py              # 页面与接口（需要 serve 在跑）
```
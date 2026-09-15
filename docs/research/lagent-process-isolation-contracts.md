# 本机候选进程边界（LE-008 增量）

`advisor/research/experiments/isolation.py` 提供内部 `DarwinCandidateRunner`。
这是已经用真实本地 Python 进程验证的实验后端；尚不是正式可运行的 Episode，
`capabilities()` 和每次结果的 `formal_ready` 均为 false。

## 启动与信息边界

宿主用 `DarwinPythonRuntime.discover()` 发现当前 macOS framework Python 的实际
解释器、标准库和本地动态库依赖。未知相对依赖、缺少 sandbox-exec 或不支持的平台
失败关闭，不退回无沙箱执行。依赖发现不安装软件、不下载文件。解释器及标准库仍是
宿主安装，尚未形成封存的运行时镜像，不能把此能力用于正式比较的环境指纹。

`run(package_hash, payload, limits=..., cancelled=...)` 从 ArtifactStore 重新验证
封存 manifest/内容，为每次执行建立私有目录。只把 manifest 文件写入只读 package，
用独占创建拒绝大小写/Unicode 别名覆盖；原源码之后修改不影响执行。另建空 scratch，
不挂载工作区、数据仓库、其他 Test 或宿主凭据。依赖锁仅作为封存文件提供，当前只
支持标准库与包内代码；不自动安装候选声明的依赖。

内核策略 default deny，只允许固定 Python 的 exec、指定系统运行库/标准库/包读取，
以及本次 scratch 读写。标准库 site-packages 显式禁止。动态库使用具体路径，
不放开整个第三方库目录。dyld 启动需要根目录自身的读取；这会暴露 `/` 的直接目录
项，不授予 `/Users` 等目录的遍历或文件内容。没有网络、Mach lookup、sysctl、
fork、任意程序执行或硬链接授权。软链接不扩大目标路径权限。

子进程只继承 stdin/stdout/stderr 三条管道，`close_fds=True`、空 pass_fds；环境仅
含 LANG/TMPDIR。Python 使用 `-I -S -B`，先执行宿主 bootstrap 设置资源限制，
再加载候选入口。传入 payload 必须已经通过宿主时间/权限过滤；输出仍是候选不可信
字节，本层不把它当合法 action、交易计划、记忆或完成报告。

## 资源与退出

ProcessLimits 没有默认额度，调用方须显式给出秒数、CPU 秒、输入/合并输出字节、
单文件/总 scratch 字节、目录项数及 FD 数。当前这些参数还没有接入封存实验的资源
包络或预算授权，不能用测试中的数值代替原例参数。

- CPU、单文件大小及 FD 数通过 setrlimit 设置软/硬值，core dump 禁用。macOS
  的 SIGXCPU 可以被捕获/忽略，因此不声明已证明 CPU 硬费用上界。
- wall deadline、scratch 总字节/目录项和输出由宿主监测，可能在采样间隔内超调；
  scratch 不是文件系统硬配额。检查使用目录 FD 和 O_NOFOLLOW，候选重命名/软链接
  替换不能让宿主跟随路径读取包外目录；无法检查时停止。
- stdout 与 stderr 共用字节上限；无换行输出和不读 stdin 不会阻塞宿主。保留字节
  始终有界，超限进入停止。这里没有交互式 JSON 帧/工具执行循环。
- 超时、取消、资源超限先 SIGTERM，配置宽限后 SIGKILL。宿主回调异常也在 finally
  终止并等待子进程。只有对该 PID 的 wait4 成功回收后才产生 quiescent=true，随后
  清理临时文件。记录该进程 user/system CPU 秒和 macOS peak RSS 字节；RSS 只有
  退出时测量，没有硬内存限制，也没有自动记入 CostBudget。

进程启动前的 materialization/依赖发现费用、宿主监测开销及失败路径的持久用量仍待
资源计量集成。宿主 SIGKILL/崩溃后的孤儿回收、重启身份核对、阶段 fence、持久进程
清理记录和内核拒绝事件审计也尚未完成；不能把普通 finally 回收当作崩溃恢复验收。

## 平台限制与验证

Apple DTS 明确说明 sandbox-exec 已弃用，第三方编写 SBPL 策略不属于支持的接口。
这是采用本机后端进行开发验证的限制，不是跨版本安全保证。[Apple 原始说明](https://developer.apple.com/forums/thread/661939)

`test_experiment_isolation.py` 使用真实本地进程与临时、无业务数据的哨兵文件。
覆盖封存执行、包外读写/目录读取、包修改、继承 env/FD、软硬链接、loopback TCP/
Unix socket、shell/Python 子进程/fork、CPU 信号、文件限额、总 scratch 超限、
不可检查目录、stdout/stderr 洪泛、忽略 SIGTERM 的强制回收、宿主取消/异常和缺能力。
未连接真实模型、搜索、生产数据库或外部网络。非 macOS 不以 mock 代替这些验证。

正式隔离、主子研究循环、阶段/预算接线、生产模型驱动和完整对抗/崩溃恢复验收仍
属于 LE-008/009/010/014/015 的未完成项。本后端没有新增 API 或 CLI；skill/workflows
同步说明能力边界，CLI catalog 保持现有 51 项。

后续增量：底层 run 现在可连接有界 ToolChannel，PhaseCandidateProcesses 已提供
阶段校验、双向网关调用、持久启动/退出记录及阶段清理等待。上述一次性运输限制
描述的是最初独立后端；新增适配器的准确范围及恢复缺口见
[阶段工具管道契约](lagent-phase-process-contracts.md)。预算/模型/正式隔离门槛仍未完成。

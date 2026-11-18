# go2rtc mp4/MSE 播放 panic 根因与根治

CMCC S4602 / RV1109 设备上的 go2rtc v1.9.15 一播放（mp4/MSE 消费路径）就 SIGSEGV 崩溃，
客户端播放时进程被自己打死。本文记录根因、上游 PR #2339 为何不够、以及 S4602 自研的
增强补丁如何根治，含 GA 可复现构建。

## 1. 现象

- 崩溃栈（原版 v1.9.15，无补丁）：
  ```
  panic: runtime error: invalid memory address or nil pointer dereference
  [signal SIGSEGV: code=0x1 addr=0xc]
  bufio.(*Writer).Write
  net/http.(*response).Write
  core.(*WriteBuffer).Write            // writebuffer.go:33
  mp4.(*Consumer).AddTrack.func1
  h264.RepairAVCC.func1
  ```
- 触发条件：客户端拉 mp4/MSE 流（`/api/stream.mp4?src=camera1`）播放后断开。
- 流链路：`h264out --mirror 127.0.0.1:5602 → rtp2pipe 解包裸 H264 → go2rtc camera1(exec) → mp4/MSE/RTSP`。

## 2. 根因（TOCTOU 竞态）

`pkg/core/writebuffer.go` 的 `WriteBuffer` 把 track sender 的异步写和
`net/http.ResponseWriter` 桥接。handler 返回后，net/http 会**立即回收**该
response 的 `bufio.Writer`。但客户端断开时，sender 协程可能仍停在
`Write()` 里，此时：

- `Write()` 内 `w.Writer.Write(p)`（:33）或 `f.Flush()`（:37）访问的是**已被回收**
  的 bufio.Writer → 读已释放对象 → `addr=0xc` nil deref。

本质是 net/http 层回收 response 早于我方任何 `Close()`，无法用锁/标志预判，
是典型的 use-after-recycle TOCTOU。

## 3. 上游 PR #2339 为何不够

PR #2339（open 未合并）只改了 `Close()`：持锁读 `w.Writer`，若无可 Close 的
writer 就设 `w.err = io.ErrClosedPipe` 阻止"迟到写"。它的回归测试用
`recyclableWriter` **只模拟了 `Write`、没模拟 `http.Flusher`**。

S4602 实机套上 PR #2339 后**依然崩溃**，且崩溃点从 `:33 (Write)` 移到
`:37 (f.Flush)`：

| 版本 | 崩溃行 | 函数 |
|---|---|---|
| 原版 | writebuffer.go:33 | `w.Writer.Write` |
| 仅 PR#2339 | writebuffer.go:37 | `f.Flush` |

`Close()` 设 `w.err` 挡不住锁竞争：panic 发生在 `Write()` 已拿到 `w.mu`、
但 `w.err` 尚未被 `Close()` 置位、且底层 response 已被 net/http 回收的瞬间。

## 4. S4602 增强补丁（writebuffer-flush-guard.patch）

在 PR #2339 之上，再给 `Write()` 加 `defer recover()`，同时堵住 `Write`(:33) 和
`Flush`(:37) 两个崩溃点：

- `Write()` 开头 `defer func(){ if r:=recover(); r!=nil { 标记 closed; w.done() } ; w.mu.Unlock() }()`
  - panic 被吞掉，进程不死（最坏丢一帧）。
  - `Unlock` 移进 defer，保证 panic 后锁必然释放，后续 `Write`/`Close` 不死锁。
  - 置 `w.err=io.ErrClosedPipe`，让 sender 后续写全部短路返回。

顺序：必须先 `git apply writebuffer-close.patch`（PR#2339）再 apply
`writebuffer-flush-guard.patch`（后者 hunk 基线是"Close 已改、Write 未改"状态）。

## 5. 回归测试（writebuffer_test.go，3 个）

| 测试 | 覆盖 |
|---|---|
| `TestWriteBufferCloseBlocksLateWrites` | PR#2339 场景：handler 返回后 Write 不再碰回收 writer |
| `TestWriteBufferFlushPanicRecovered` | `panicFlusher` 模拟回收 response 的 Flush panic，Write 必须 recover 返回 err |
| `TestWriteBufferPanicReleasesLock` | panic 后锁释放，后续 Write/Close 不死锁（2s 超时判死锁） |

未打增强补丁时后两个测试会直接 crash 整个测试二进制。

## 6. GA 可复现构建

`.github/workflows/build-go2rtc-patched.yml`：

1. clone go2rtc v1.9.15（`board/s4602/go2rtc/` 外的原始源码）。
2. `git apply` close patch + flush-guard patch。
3. 删 v1.9.15 tag 自带坏测试 `core_test.go`（引用未定义 `StripUserinfo`，tag 与测试不同步）。
4. `go test ./pkg/core/ -run 'TestWriteBuffer'` —— 3 测试全 PASS 才继续（Gate 1）。
5. `GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 go build -ldflags "-s -w" -trimpath`（Go 1.27）。
6. Gate 2：断言 ELF32 EM_ARM(40) armhf。
7. 上传 artifact `go2rtc-linux-arm-patched`。

实跑：
- run 44258779862 **全绿**，3 测试 PASS，ELF Gate 通过，artifact 7.96MB。
- 部署：设备 `/data` 是 17.5M UBIFS（易满），故 xz 压到 5.45MB 存 `/data/www/go2rtc.xz`，
  bootfix 已有 `xz -dkc` 解压逻辑；`/tmp` 是 tmpfs 重启清空。
- **实机播放回归**：`/api/stream.mp4?src=camera1` 连播 3 轮 + 压力 5 轮（各 ~1.5–2MB 后突发
  断开）全部成功，进程存活、`grep -c panic /tmp/go2rtc.log = 0`，RTSP 正常响应。

## 7. 运维缺口（待办）

bootfix 里 go2rtc **没有守护**（swapp 有 guard，go2rtc 没有）：进程退出后无人自动拉起。
增强补丁已根治已知 panic，但通用 watchdog 仍建议补上（bootfix 加循环，`kill -0` 探活，
崩了 `xz -dkc` 重新起），作为未知崩溃的兜底。

## 8. 一句话教训

- 单点打补丁不可靠：PR #2339 只堵了 `Write` 没堵 `Flush`，测试也没覆盖 `Flusher`。
- 根治 TOCTOU use-after-recycle 只能 `defer recover`（writer 回收在我方控制之外）。
- 崩溃点会"搬家"：修掉 A 处 panic，B 处同类隐患立即暴露——要按整条调用链堵。

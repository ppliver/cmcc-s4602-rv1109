/* webd.c — S4602 极简 Web 配置守护进程
 * 参考 OpenIPC/waybeam 的内置 HTTP 思路：
 *   GET /                → /data/www/index.html
 *   GET /api/status      → JSON 状态（推流进程/实测fps/延迟/云链路/para）
 *   GET /api/stream      → ?action=start|stop&res=720p|1080p|1440p&kbps=500..6000&fps=15|20|25|30&gop=10..120
 *   GET /api/cloud       → ?on=1|0  (默认路由开关 = 云链路开关)
 *   GET /api/modules     → ?go2rtc=on|off  (go2rtc+rtp2pipe 开关, 持久化 modules.conf;
 *                          无参数 = 查询两者状态)
 * 静态编译，busybox sh 执行外部动作。仅限内网使用。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <fcntl.h>

#define PORT    8080
#define DOCROOT "/data/www"
#define CONF    "/data/www/stream.conf"
#define LOG     "/tmp/h264out.log"
#define RTLIBTGZ "/data/www/rtlib.tar.gz"
#define BIN     "/data/www/h264out_v4"
#define GW      "192.168.6.1"
#define DST_IP  "192.168.6.88"
#define DST_PORT 5600

/* ---------- 小工具 ---------- */
static int readfile(const char *p, char *buf, int n) {
    FILE *f = fopen(p, "r"); if (!f) return -1;
    int k = (int)fread(buf, 1, n - 1, f); fclose(f);
    buf[k > 0 ? k : 0] = 0; return k;
}
/* popen 输出取最后一行 */
static void lastline(const char *cmd, char *out, int n) {
    out[0] = 0;
    FILE *f = popen(cmd, "r"); if (!f) return;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        size_t L = strlen(line);
        while (L && (line[L-1] == '\n' || line[L-1] == '\r')) line[--L] = 0;
        snprintf(out, n, "%s", line);
    }
    pclose(f);
}
static int run(const char *cmd) { return system(cmd); }

/* ---------- 推流进程控制 ---------- */
static int stream_pid(void) {
    FILE *f = popen("ps w | grep '[h]264out' | grep -v grep", "r");
    if (!f) return 0;
    char line[512]; int pid = 0;
    while (fgets(line, sizeof line, f)) {
        /* "  123 root  0:01 /tmp/rtlib/... h264out_v4 ..." */
        char *p = line; while (*p == ' ' || *p == '\t') p++;
        pid = atoi(p);
        if (pid > 1) break;
    }
    pclose(f); return pid;
}
static void stream_stop(void) {
    int pid = stream_pid();
    if (pid <= 1) return;
    char cmd[128];
    snprintf(cmd, sizeof cmd, "kill %d 2>/dev/null", pid);
    run(cmd);
    /* 等待进程真正消失（最多 6s），不消失则 kill -9 兜底 */
    for (int i = 0; i < 12; i++) {
        sleep(1);
        int cur = stream_pid();
        if (cur <= 1 || cur == pid) {
            if (cur == pid) {
                snprintf(cmd, sizeof cmd, "kill -9 %d 2>/dev/null", pid);
                run(cmd);
                sleep(1);
            }
            break;
        }
    }
    sleep(1);   /* 给 v4l2 节点释放留时间 */
}
static void stream_start(int w, int h, int fps, int kbps, int gop, const char *codec,
                         const char *ip, int port, int pace_ms, int cir) {
    /* 断电恢复路径：/tmp 资产从 /data/www 重建 */
    if (access("/tmp/rtlib/rtlib/ld-linux-armhf.so.3", X_OK) != 0) {
        run("mkdir -p /tmp/rtlib && tar xzf " RTLIBTGZ " -C /tmp/rtlib");
    }
    run("mkdir -p /tmp/factory/lib && ln -sf /system/lib/librockchip_mpp.so.1 /tmp/factory/lib/librockchip_mpp.so.1");
    /* 始终从 /data/www 刷新（覆盖旧版），保证"应用"立即用上新二进制，无需重启设备 */
    run("cp " BIN " /tmp/h264out_v4 && chmod +x /tmp/h264out_v4");
    char cmd[640];
    /* 1080p = 1440p 旁路采集 + 中心裁切（scale2 缩放档上限 1280 宽，无法直接出 1080p） */
    char capw[64] = "";
    if (w == 1920 && h == 1080) snprintf(capw, sizeof capw, " --capw 2560 --caph 1440");
    /* --mirror 127.0.0.1:5602: go2rtc camera1 源 (exec:rtp2pipe 5602) 全靠 h264out 的
     * 第二个 UDP 目的(127.0.0.1:5602)喂裸流。bootfix.sh 有、webd 重启路径漏了 ->
     * 只要 h264out 被 /api/stream 重启过, rtp2pipe 收不到流, webrtc 空屏。补齐。
     * 注意: 必须写成 IP:PORT 单 token（不能拆成 "IP PORT"），h264out.c 只认单 token。 */
    snprintf(cmd, sizeof cmd,
        "/tmp/rtlib/rtlib/ld-linux-armhf.so.3 "
        "--library-path /tmp/rtlib/rtlib:/system/lib:/lib "
        "/tmp/h264out_v4 --src nv12 --dev /dev/video21 --ip %s --port %d "
        "--w %d --h %d --fps %d --kbps %d --gop %d --codec %s --pace %d --cir %d%s "
        "--mirror 127.0.0.1:5602 > " LOG " 2>&1 &",
        ip, port, w, h, fps, kbps, gop, codec, pace_ms * 1000, cir, capw);
    run(cmd);
    /* 启动确认：2s 后检查进程在不在，不在则重试一次 */
    sleep(2);
    if (stream_pid() <= 1) {
        run(cmd);
        sleep(2);
    }
}

/* ---------- 参数校验 ---------- */
static int parse_stream_params(const char *q, int *w, int *h, int *fps, int *kbps, int *gop,
                               char *codec, int cn, char *ip, int ipn, int *port, int *pace,
                               int *cir, char *msg, int mn) {
    /* 默认沿用配置 */
    *w = 1280; *h = 720; *fps = 25; *kbps = 1500; *gop = 25; *pace = 3; *cir = 2;
    snprintf(codec, cn, "h264");
    snprintf(ip, ipn, "%s", DST_IP);
    *port = DST_PORT;
    char buf[512]; snprintf(buf, sizeof buf, "%s", q ? q : "");
    char *save = NULL, *tok;
    for (tok = strtok_r(buf, "&", &save); tok; tok = strtok_r(NULL, "&", &save)) {
        char *eq = strchr(tok, '='); if (!eq) continue;
        *eq = 0; const char *k = tok, *v = eq + 1;
        int iv = atoi(v);
        if (!strcmp(k, "res")) {
            if (!strcmp(v, "1440p"))     { *w = 2560; *h = 1440; }   /* ISPP 旁路：等于输入尺寸，驱动免缩放放行 */
            else if (!strcmp(v, "1080p")){ *w = 1920; *h = 1080; }   /* 1440p 旁路采集 + 中心裁切 */
            else if (!strcmp(v, "720p"))      { *w = 1280; *h = 720; }
            else { snprintf(msg, mn, "bad res"); return -1; }
        } else if (!strcmp(k, "codec")) {
            if (!strcmp(v, "h264") || !strcmp(v, "h265")) snprintf(codec, cn, "%s", v);
            else { snprintf(msg, mn, "codec must be h264|h265"); return -1; }
        } else if (!strcmp(k, "kbps")) {
            if (iv < 500 || iv > 6000) { snprintf(msg, mn, "kbps out of 500..6000"); return -1; }
            *kbps = iv;
        } else if (!strcmp(k, "fps")) {
            if (iv != 15 && iv != 20 && iv != 25 && iv != 30) { snprintf(msg, mn, "fps must be 15/20/25/30"); return -1; }
            *fps = iv;
        } else if (!strcmp(k, "gop")) {
            if (iv < 10 || iv > 120) { snprintf(msg, mn, "gop out of 10..120"); return -1; }
            *gop = iv;
        } else if (!strcmp(k, "port")) {
            if (iv < 1024 || iv > 65535) { snprintf(msg, mn, "port out of 1024..65535"); return -1; }
            *port = iv;
        } else if (!strcmp(k, "pace")) {
            /* 弱网平滑窗口 ms；0 = 纯突发（链路好时延迟最低） */
            if (iv < 0 || iv > 20) { snprintf(msg, mn, "pace out of 0..20 ms"); return -1; }
            *pace = iv;
        } else if (!strcmp(k, "cir")) {
            /* 循环帧内刷新 MB 行/帧；0 = 关闭 */
            if (iv < 0 || iv > 8) { snprintf(msg, mn, "cir out of 0..8"); return -1; }
            *cir = iv;
        } else if (!strcmp(k, "ip")) {
            /* IPv4 点分十进制严格校验 */
            unsigned a=0,b=0,c=0,d=0; char tail[8];
            if (sscanf(v, "%u.%u.%u.%u%7s", &a,&b,&c,&d,tail) != 4 ||
                a>255 || b>255 || c>255 || d>255) {
                snprintf(msg, mn, "bad ip"); return -1;
            }
            snprintf(ip, ipn, "%u.%u.%u.%u", a,b,c,d);
        }
    }
    return 0;
}

/* ---------- HTTP 响应 ---------- */
static void hdr(int fd, int code, const char *ctype) {
    char h[256];
    int n = snprintf(h, sizeof h,
        "HTTP/1.0 %d %s\r\nContent-Type: %s\r\nCache-Control: no-store\r\n"
        "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
        code, code == 200 ? "OK" : "ERR", ctype);
    write(fd, h, n);
}
static void send_json(int fd, const char *json) {
    hdr(fd, 200, "application/json");
    write(fd, json, strlen(json));
}
static void send_err(int fd, const char *msg) {
    char b[256];
    snprintf(b, sizeof b, "{\"ok\":0,\"msg\":\"%s\"}", msg);
    send_json(fd, b);
}
static void send_file(int fd, const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { hdr(fd, 404, "text/plain"); write(fd, "not found", 9); return; }
    hdr(fd, 200, "text/html; charset=utf-8");
    char buf[8192]; size_t k;
    while ((k = fread(buf, 1, sizeof buf, f)) > 0) write(fd, buf, k);
    fclose(f);
}

/* ---------- /api/status ---------- */
static void api_status(int fd) {
    char stat[512] = "", lat[512] = "", route[256] = "", para[1024] = "";
    char conf[256] = "";
    lastline("grep -a '\\[stat\\]' " LOG " 2>/dev/null | tail -1", stat, sizeof stat);
    lastline("grep -a '\\[lat\\]' " LOG " 2>/dev/null | tail -1", lat, sizeof lat);
    lastline("route -n | grep '^0.0.0.0'", route, sizeof route);
    /* para 只取关键字段 */
    lastline("grep -aE 'main_stream_size|main_bitrate|main_stream_fps|main_stream_gop|sub_stream_fps' /data/.swdb/para 2>/dev/null | tr '\\n' ';' ", para, sizeof para);
    readfile(CONF, conf, sizeof conf);
    /* conf 是多行 k=v，嵌入 JSON 需把换行转义为 \n 字面量（不是替换成空格：
     * 前端按行 split 回填表单，空格化导致 fps/gop/分辨率回填全错位） */
    char confj[600]; int ci = 0;
    for (char *p = conf; *p && ci < (int)sizeof confj - 2; p++) {
        if (*p == '\n')      { confj[ci++] = '\\'; confj[ci++] = 'n'; }
        else if (*p == '\r') { /* skip */ }
        else if (*p == '"')  { confj[ci++] = '\\'; confj[ci++] = '"'; }
        else confj[ci++] = *p;
    }
    confj[ci] = 0;
    int pid = stream_pid();
    char up[64] = "";
    { char ub[64]; readfile("/proc/uptime", ub, sizeof ub); char *sp = strchr(ub, ' '); if (sp) *sp = 0; snprintf(up, sizeof up, "%.0f", atof(ub)); }
    char ip[64] = "";
    lastline("ifconfig eth0 2>/dev/null | grep 'inet addr' | sed 's/.*addr://; s/ .*//'", ip, sizeof ip);

    char *j = malloc(4096); int n = 0;
    n = snprintf(j, 4096,
        "{\"ok\":1,\"pid\":%d,\"ip\":\"%s\",\"uptime\":%s,"
        "\"cloud\":\"%s\","
        "\"stat\":\"%s\",\"lat\":\"%s\","
        "\"para\":\"%s\",\"conf\":\"%s\"}",
        pid, ip, up,
        route[0] ? "on" : "off",
        stat, lat, para, confj);
    send_json(fd, j);
    free(j);
}

/* ---------- /api/stream ---------- */
static void api_stream(int fd, const char *q) {
    char msg[128] = "";
    char act[16] = "apply";
    char buf[512]; snprintf(buf, sizeof buf, "%s", q ? q : "");
    char *save = NULL, *tok;
    for (tok = strtok_r(buf, "&", &save); tok; tok = strtok_r(NULL, "&", &save)) {
        char *eq = strchr(tok, '=');
        if (eq && !strncmp(tok, "action", 6)) { *eq = 0; snprintf(act, sizeof act, "%s", eq + 1); }
    }
    if (!strcmp(act, "stop")) {
        if (stream_pid() <= 0) { send_err(fd, "not running"); return; }
        stream_stop();
        send_json(fd, "{\"ok\":1,\"msg\":\"stream stopped\"}");
        return;
    }
    if (strcmp(act, "apply") && strcmp(act, "start")) { send_err(fd, "bad action"); return; }
    int w, h, fps, kbps, gop, port, pace, cir;
    char codec[8], ip[24];
    if (parse_stream_params(q, &w, &h, &fps, &kbps, &gop, codec, sizeof codec,
                            ip, sizeof ip, &port, &pace, &cir, msg, sizeof msg) != 0) {
        send_err(fd, msg); return;
    }
    stream_stop();
    /* 持久化配置 */
    FILE *f = fopen(CONF, "w");
    if (f) { fprintf(f, "w=%d\nh=%d\nfps=%d\nkbps=%d\ngop=%d\ncodec=%s\nip=%s\nport=%d\npace=%d\ncir=%d\n",
                     w, h, fps, kbps, gop, codec, ip, port, pace, cir); fclose(f); }
    stream_start(w, h, fps, kbps, gop, codec, ip, port, pace, cir);
    char out[224];
    snprintf(out, sizeof out, "{\"ok\":1,\"msg\":\"%dx%d@%d %s %dkbps gop%d -> %s:%d pace%dms cir%d applying\"}",
             w, h, fps, codec, kbps, gop, ip, port, pace, cir);
    send_json(fd, out);
}

/* ---------- URL 解码（%xx 与 +） ---------- */
static int hexv(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static void urldecode(char *s) {
    char *o = s;
    for (; *s; s++) {
        if (*s == '%' && hexv(s[1]) >= 0 && hexv(s[2]) >= 0) {
            *o++ = (char)(hexv(s[1]) * 16 + hexv(s[2]));
            s += 2;
        } else if (*s == '+') *o++ = ' ';
        else *o++ = *s;
    }
    *o = 0;
}

/* ---------- /api/log：推流日志尾部（诊断用） ---------- */
static void api_log(int fd) {
    hdr(fd, 200, "text/plain; charset=utf-8");
    FILE *f = popen("tail -n 200 " LOG " 2>&1", "r");
    if (!f) { write(fd, "popen fail", 10); return; }
    char buf[1024]; size_t k;
    while ((k = fread(buf, 1, sizeof buf, f)) > 0) write(fd, buf, k);
    pclose(f);
}

/* ---------- /api/exec：受限诊断命令（内网实验设备专用） ---------- */
static void api_exec(int fd, const char *q) {
    char cmd[768] = "";
    const char *p = strstr(q ? q : "", "cmd=");
    if (!p) { send_err(fd, "need cmd="); return; }
    snprintf(cmd, sizeof cmd, "%s", p + 4);
    urldecode(cmd);
    hdr(fd, 200, "text/plain; charset=utf-8");
    FILE *f = popen(cmd, "r");
    if (!f) { write(fd, "popen fail", 10); return; }
    char buf[1024]; size_t k;
    int total = 0;
    while ((k = fread(buf, 1, sizeof buf, f)) > 0 && total < 32768) { write(fd, buf, k); total += k; }
    pclose(f);
}

/* ---------- /api/cloud ---------- */
static void api_cloud(int fd, const char *q) {
    if (strstr(q, "on=1")) {
        run("route add default gw " GW " 2>/dev/null");
        send_json(fd, "{\"ok\":1,\"msg\":\"cloud route added\"}");
    } else if (strstr(q, "on=0")) {
        run("route del default 2>/dev/null");
        send_json(fd, "{\"ok\":1,\"msg\":\"cloud route removed\"}");
    } else {
        send_err(fd, "need on=0|1");
    }
}

/* ---------- /api/modules：go2rtc(+rtp2pipe) 服务开关 ----------
 * rtp2pipe 是 go2rtc 的 exec producer 子进程，单独开关无意义，
 * 一个开关控两个。持久化写 /data/www/modules.conf 的 go2rtc=0|1：
 * bootfix 开机段与 20s watchdog 都会重读该文件，off 后重启/守护
 * 都不会再拉起（省 CPU），on 后由本接口立即拉起。 */
#define MODCONF "/data/www/modules.conf"

static int pid_of(const char *name) {
    /* name 传入裸名，grep 用 [x]name 防自匹配：
     * [%c] 放首字符、%s 放 name+1（其余部分），拼出 [g]o2rtc 这类模式。
     * 坑：若 %s 传完整 name 会得到 [g]go2rtc（双 g）恒不匹配。 */
    char cmd[128];
    snprintf(cmd, sizeof cmd, "ps w | grep '[%c]%s' | grep -v grep", name[0], name + 1);
    FILE *f = popen(cmd, "r");
    if (!f) return 0;
    char line[512]; int pid = 0;
    while (fgets(line, sizeof line, f)) {
        char *p = line; while (*p == ' ' || *p == '\t') p++;
        pid = atoi(p);
        if (pid > 1) break;
    }
    pclose(f); return pid;
}
static void proc_stop(const char *name) {
    for (int i = 0; i < 6; i++) {
        int pid = pid_of(name);
        if (pid <= 1) return;
        char cmd[64];
        snprintf(cmd, sizeof cmd, "kill %d 2>/dev/null", pid);
        run(cmd);
        sleep(1);
    }
    int pid = pid_of(name);
    if (pid > 1) {
        char cmd[64];
        snprintf(cmd, sizeof cmd, "kill -9 %d 2>/dev/null", pid);
        run(cmd);
    }
}
/* 改 k=v 行（保留其它行），文件不存在则创建 */
static void conf_set(const char *path, const char *key, const char *val) {
    enum { MAXL = 16, LEN = 96 };
    static char lines[MAXL][LEN];
    int n = 0, found = 0;
    size_t kl = strlen(key);
    FILE *f = fopen(path, "r");
    if (f) {
        char line[128];
        while (fgets(line, sizeof line, f) && n < MAXL) {
            line[strcspn(line, "\r\n")] = 0;
            if (!strncmp(line, key, kl) && line[kl] == '=') {
                snprintf(lines[n++], LEN, "%s=%s", key, val);
                found = 1;
            } else if (line[0]) {
                snprintf(lines[n++], LEN, "%s", line);
            }
        }
        fclose(f);
    }
    if (!found && n < MAXL) snprintf(lines[n++], LEN, "%s=%s", key, val);
    f = fopen(path, "w");
    if (f) {
        for (int i = 0; i < n; i++) fprintf(f, "%s\n", lines[i]);
        fclose(f);
        sync();
    }
}
static void go2rtc_start(void) {
    if (pid_of("go2rtc") > 1) return;
    if (access("/tmp/go2rtc_linux_arm", X_OK) != 0) {
        if (access("/data/www/go2rtc.xz", R_OK) == 0)
            run("xz -dkc /data/www/go2rtc.xz > /tmp/go2rtc_linux_arm && chmod +x /tmp/go2rtc_linux_arm");
        else if (access("/data/www/go2rtc_linux_arm", X_OK) == 0)
            run("cp /data/www/go2rtc_linux_arm /tmp/go2rtc_linux_arm && chmod +x /tmp/go2rtc_linux_arm");
    }
    if (access("/tmp/go2rtc_linux_arm", X_OK) != 0) return;
    /* 必须 cd /data/www：go2rtc 用相对路径找 go2rtc.yaml */
    run("cd /data/www && /tmp/go2rtc_linux_arm > /tmp/go2rtc.log 2>&1 &");
}
static void api_modules(int fd, const char *q) {
    if (strstr(q, "go2rtc=off")) {
        conf_set(MODCONF, "go2rtc", "0");
        proc_stop("go2rtc");            /* 退出时由 killtimeout=5 回收 exec producer */
        sleep(2);
        proc_stop("rtp2pipe");          /* 兜底孤儿（go2rtc 未正常回收时） */
        send_json(fd, "{\"ok\":1,\"msg\":\"go2rtc+rtp2pipe stopped (go2rtc=0 persisted)\"}");
        return;
    }
    if (strstr(q, "go2rtc=on")) {
        conf_set(MODCONF, "go2rtc", "1");
        go2rtc_start();
        sleep(2);
        send_json(fd, pid_of("go2rtc") > 1
            ? "{\"ok\":1,\"msg\":\"go2rtc started (go2rtc=1 persisted)\"}"
            : "{\"ok\":0,\"msg\":\"go2rtc binary missing (go2rtc.xz?)\"}");
        return;
    }
    int g = pid_of("go2rtc"), r = pid_of("rtp2pipe");
    char j[160];
    snprintf(j, sizeof j,
        "{\"ok\":1,\"go2rtc\":\"%s\",\"go2rtc_pid\":%d,\"rtp2pipe_pid\":%d}",
        g > 1 ? "on" : "off", g > 1 ? g : 0, r > 1 ? r : 0);
    send_json(fd, j);
}

/* ---------- 连接处理 ---------- */
static void handle(int fd) {
    /* 防 fd 泄漏：connection socket 不允许被 system() 的子进程（h264out）继承 */
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    char req[2048] = "";
    int n = 0, r;
    /* 读请求头（最多 2KB，1s 超时保护由 socket 层兜底） */
    while (n < (int)sizeof req - 1) {
        char c;
        r = read(fd, &c, 1);
        if (r <= 0) break;
        req[n++] = c;
        if (n >= 4 && !strncmp(req + n - 4, "\r\n\r\n", 4)) break;
    }
    req[n] = 0;
    char method[8] = "", path[256] = "", query[1024] = "";
    if (sscanf(req, "%7s %255s", method, path) != 2) return;
    if (strcmp(method, "GET")) { hdr(fd, 405, "text/plain"); return; }
    char *qm = strchr(path, '?');
    if (qm) { snprintf(query, sizeof query, "%s", qm + 1); *qm = 0; }

    if (!strcmp(path, "/") || !strcmp(path, "/index.html")) send_file(fd, DOCROOT "/index.html");
    else if (!strcmp(path, "/api/status")) api_status(fd);
    else if (!strcmp(path, "/api/stream")) api_stream(fd, query);
    else if (!strcmp(path, "/api/cloud"))  api_cloud(fd, query);
    else if (!strcmp(path, "/api/modules")) api_modules(fd, query);
    else if (!strcmp(path, "/api/log"))    api_log(fd);
    else if (!strcmp(path, "/api/exec"))   api_exec(fd, query);
    else { hdr(fd, 404, "text/plain"); write(fd, "not found", 9); }
}

int main(void) {
    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_IGN);
    int ls = socket(AF_INET, SOCK_STREAM, 0);
    if (ls < 0) { perror("socket"); return 1; }
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = INADDR_ANY;
    a.sin_port = htons(PORT);
    if (bind(ls, (struct sockaddr *)&a, sizeof a) < 0) { perror("bind"); return 1; }
    if (listen(ls, 8) < 0) { perror("listen"); return 1; }
    fprintf(stderr, "[webd] listening on :%d docroot=%s\n", PORT, DOCROOT);
    for (;;) {
        int c = accept(ls, NULL, NULL);
        if (c < 0) continue;
        if (fork() == 0) {
            close(ls);
            alarm(10);          /* 慢客户端保护 */
            handle(c);
            close(c);
            _exit(0);
        }
        close(c);
    }
    return 0;
}

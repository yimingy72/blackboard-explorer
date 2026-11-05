#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <linux/capability.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define SOLVER_UID 10000
#define SOLVER_GID 10000
#define CONTROL_DIR "/var/lib/bbx/control/launches"

static void fail(const char *step) {
    fprintf(stderr, "bbx launcher: %s: %s\n", step, strerror(errno));
    _exit(126);
}

static int identifier(const char *value) {
    size_t size = strlen(value);
    if (size == 0 || size > 80) return 0;
    for (size_t i = 0; i < size; i++) {
        char c = value[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) return 0;
    }
    return 1;
}

static int control_directory(const char *path) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) fail("open control directory");
    struct stat info;
    if (fstat(fd, &info) || info.st_uid != 0 || (info.st_mode & 0022)) {
        errno = EPERM;
        fail("unsafe control directory");
    }
    return fd;
}

static void close_extra_fds(void) {
    if (syscall(SYS_close_range, 3U, UINT_MAX, 0U) == 0) return;
    if (errno != ENOSYS && errno != EINVAL) fail("close inherited descriptors");
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit)) fail("descriptor limit");
    for (rlim_t fd = 3; fd < limit.rlim_cur; fd++) close((int)fd);
}

static unsigned long long start_ticks(void) {
    char line[4096];
    FILE *source = fopen("/proc/self/stat", "re");
    if (!source || !fgets(line, sizeof(line), source)) fail("read process identity");
    fclose(source);
    char *cursor = strrchr(line, ')');
    if (!cursor) { errno = EINVAL; fail("parse process identity"); }
    cursor += 2;
    for (int field = 3; field < 22; field++) {
        cursor = strchr(cursor, ' ');
        if (!cursor) { errno = EINVAL; fail("parse process identity"); }
        cursor++;
    }
    return strtoull(cursor, NULL, 10);
}

static void drop_identity(int raw) {
    if (prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)) fail("clear ambient caps");
    for (int capability = 0; capability < 64; capability++) {
        int present = prctl(PR_CAPBSET_READ, capability, 0, 0, 0);
        if (present < 0 && errno == EINVAL) break;
        if (present < 0) fail("read bounding caps");
        if (raw && capability == CAP_NET_RAW) continue;
        if (prctl(PR_CAPBSET_DROP, capability, 0, 0, 0)) fail("drop bounding caps");
    }
    if (prctl(PR_SET_KEEPCAPS, raw, 0, 0, 0) || setgroups(0, NULL) ||
        setresgid(SOLVER_GID, SOLVER_GID, SOLVER_GID) ||
        setresuid(SOLVER_UID, SOLVER_UID, SOLVER_UID)) fail("drop solver identity");
    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct caps[2] = {{0}, {0}};
    uint32_t expected = raw ? (1U << CAP_NET_RAW) : 0;
    caps[0].effective = caps[0].permitted = caps[0].inheritable = expected;
    if (syscall(SYS_capset, &header, caps)) fail("set solver capabilities");
    if (raw && prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, CAP_NET_RAW, 0, 0)) fail("retain raw capability");
    if (prctl(PR_SET_KEEPCAPS, 0, 0, 0, 0) ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) fail("restrict solver privileges");
    if (syscall(SYS_capget, &header, caps) || caps[0].effective != expected || caps[1].effective ||
        caps[0].permitted != expected || caps[1].permitted ||
        caps[0].inheritable != expected || caps[1].inheritable ||
        getuid() != SOLVER_UID || geteuid() != SOLVER_UID ||
        getgid() != SOLVER_GID || getegid() != SOLVER_GID ||
        prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1) { errno = EPERM; fail("verify identity"); }
}

static void add_environment(char **environment, int *size, const char *key, const char *value) {
    if (asprintf(&environment[(*size)++], "%s=%s", key, value) < 0) fail("build solver environment");
}

static void start_execd(void) {
    const char *token = getenv("EXECD_ACCESS_TOKEN");
    if (getuid() != 0 || geteuid() != 0 || !token || !*token || strpbrk(token, "\r\n") ||
        strspn(token, " \t") == strlen(token)) {
        errno = EPERM;
        fail("a nonempty execd token and trusted root startup are required");
    }
    char *environment[6] = {0};
    int count = 0;
    add_environment(environment, &count, "EXECD_ACCESS_TOKEN", token);
    add_environment(environment, &count, "EXECD_ENVS", "/var/lib/bbx/control/execd.env");
    add_environment(environment, &count, "PATH", "/usr/local/bin:/usr/bin:/bin");
    add_environment(environment, &count, "HOME", "/root");
    if (clearenv()) fail("clear execd startup environment");
    close_extra_fds();
    int directory = control_directory("/var/lib/bbx/control");
    int env_file = openat(directory, "execd.env", O_RDONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    struct stat info;
    if (env_file < 0 || fstat(env_file, &info) || info.st_uid != 0 || (info.st_mode & 0077)) {
        errno = EPERM;
        fail("unsafe execd environment file");
    }
    close(env_file);
    close(directory);
    char *args[] = {"/opt/bbx/control/execd", "-init", "-port", "44772", "-log-level", "3", NULL};
    execve(args[0], args, environment);
    fail("exec official daemon");
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--execd")) start_execd();
    /* Static linkage prevents loader variables from acting before clearenv. */
    if (clearenv()) fail("clear control environment");
    if (geteuid() != 0 || getuid() != 0 || argc != 10 ||
        !identifier(argv[1]) || !identifier(argv[2]) || !identifier(argv[3]) ||
        (strcmp(argv[9], "basic") && strcmp(argv[9], "net-raw")) ||
        strncmp(argv[5], "/workspace", 10) || (argv[5][10] && argv[5][10] != '/')) {
        errno = EPERM;
        fail("invalid trusted launch request");
    }
    int directory = control_directory("/run/netns");
    int namespace_fd = openat(directory, argv[1], O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (namespace_fd < 0 || setns(namespace_fd, CLONE_NEWNET)) fail("enter assignment network");
    close(namespace_fd);
    close(directory);
    close_extra_fds();

    directory = control_directory(CONTROL_DIR);
    char filename[96];
    if (snprintf(filename, sizeof(filename), "%s.json", argv[2]) >= (int)sizeof(filename)) {
        errno = EINVAL;
        fail("evidence filename");
    }
    int evidence = openat(directory, filename, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (evidence < 0) fail("create launch evidence");
    close(directory);
    struct stat namespace_info;
    if (stat("/proc/self/ns/net", &namespace_info)) fail("read network identity");
    unsigned long long ticks = start_ticks();
    int raw = !strcmp(argv[9], "net-raw");
    drop_identity(raw);
    if (chdir(argv[5])) fail("enter solver directory");
    umask(0022);
    if (dprintf(evidence,
        "{\"command_id\":\"%s\",\"network_identity\":\"%s\",\"pid\":%d,\"pgid\":%d,"
        "\"start_ticks\":%llu,\"netns_inode\":%llu,\"uid\":%d,\"gid\":%d,"
        "\"no_new_privs\":true,\"capabilities\":\"%s\"}\n",
        argv[2], argv[3], getpid(), getpgrp(), ticks,
        (unsigned long long)namespace_info.st_ino, getuid(), getgid(), raw ? "net-raw" : "none") < 0 ||
        fsync(evidence) || close(evidence)) fail("persist launch evidence");

    char *environment[32] = {0};
    int count = 0;
    add_environment(environment, &count, "PATH", "/usr/local/bin:/usr/bin:/bin");
    add_environment(environment, &count, "HOME", "/workspace");
    add_environment(environment, &count, "USER", "solver");
    add_environment(environment, &count, "LOGNAME", "solver");
    add_environment(environment, &count, "LANG", "C.UTF-8");
    add_environment(environment, &count, "TMPDIR", "/workspace/.tmp");
    add_environment(environment, &count, "BBX_COMMAND_ID", argv[2]);
    add_environment(environment, &count, "BBX_NETWORK_ID", argv[3]);
    add_environment(environment, &count, "BBX_AGENT_IP", argv[4]);
    if (strcmp(argv[6], "-")) {
        const char *keys[] = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"};
        for (int i = 0; i < 6; i++) add_environment(environment, &count, keys[i], argv[6]);
    }
    if (strcmp(argv[7], "-")) {
        const char *keys[] = {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO"};
        for (int i = 0; i < 5; i++) add_environment(environment, &count, keys[i], argv[7]);
    }
    char *shell[] = {"/bin/bash", "--noprofile", "--norc", "-c", argv[8], NULL};
    execve(shell[0], shell, environment);
    fail("exec solver shell");
}

#define _GNU_SOURCE
#include "assignment_scope.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/magic.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/mount.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <time.h>
#include <unistd.h>

#ifndef STATX_MNT_ID_UNIQUE
#define STATX_MNT_ID_UNIQUE 0x00004000U
#endif

#define ROOT "/var/lib/bbx/control"
#define MOUNT ROOT "/cgroup2"
#define STORE ROOT "/process-scopes"
#define BINDING_LIMIT 4096

struct identity {
    char boot[37];
    unsigned long long cgroup_namespace, mount_namespace, mount_id;
    unsigned long long mount_device, mount_inode, scope_device, scope_inode;
};

static int private_directory(int fd) {
    struct stat info;
    return fstat(fd, &info) == 0 && S_ISDIR(info.st_mode) &&
        info.st_uid == 0 && !(info.st_mode & 0077);
}

static int open_private(const char *path) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd >= 0 && !private_directory(fd)) { close(fd); errno = EPERM; return -1; }
    return fd;
}

static int open_file(int directory, const char *name, int flags) {
    int fd = openat(directory, name, flags | O_NOFOLLOW | O_CLOEXEC, 0600);
    struct stat info;
    if (fd >= 0 && (fstat(fd, &info) || !S_ISREG(info.st_mode) ||
        info.st_uid != 0 || (info.st_mode & 0077))) {
        close(fd); errno = EPERM; return -1;
    }
    return fd;
}

static int write_all(int fd, const char *data, size_t size) {
    while (size) {
        ssize_t count = write(fd, data, size);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return -1;
        data += count; size -= (size_t)count;
    }
    return 0;
}

static int read_text(int fd, char *buffer, size_t size) {
    size_t used = 0;
    while (used < size - 1) {
        ssize_t count = read(fd, buffer + used, size - 1 - used);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) return -1;
        if (count == 0) { buffer[used] = 0; return 0; }
        used += (size_t)count;
    }
    errno = EOVERFLOW;
    return -1;
}

static int path_text(const char *path, char *buffer, size_t size) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    int result = read_text(fd, buffer, size);
    close(fd);
    return result;
}

static int identifier(const char *value) {
    return value && strlen(value) > 0 && strlen(value) <= 80 &&
        strspn(value, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") == strlen(value);
}

static int persist_text(int directory, const char *name, const char *value) {
    int fd = open_file(directory, name, O_WRONLY | O_CREAT | O_EXCL);
    if (fd < 0) return -1;
    int result = write_all(fd, value, strlen(value)) || fsync(fd);
    close(fd);
    return result || fsync(directory) ? -1 : 0;
}

static int environment_matches(const char *pod, const char *revision, const char *incarnation) {
    int store = open_private(STORE);
    if (store < 0) return 0;
    const char *names[] = {"pod_uid", "environment_revision", "incarnation"};
    const char *values[] = {pod, revision, incarnation};
    for (int i = 0; i < 3; i++) {
        char text[82];
        int fd = open_file(store, names[i], O_RDONLY);
        if (fd < 0 || read_text(fd, text, sizeof(text)) || strcmp(text, values[i])) {
            if (fd >= 0) close(fd);
            close(store); return 0;
        }
        close(fd);
    }
    close(store);
    return 1;
}

static int create_incarnation(int store) {
    unsigned char random[32];
    size_t used = 0;
    while (used < sizeof(random)) {
        ssize_t count = getrandom(random + used, sizeof(random) - used, 0);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return -1;
        used += (size_t)count;
    }
    char hex[65];
    for (size_t i = 0; i < sizeof(random); i++) snprintf(hex + i * 2, 3, "%02x", random[i]);
    return persist_text(store, "incarnation", hex);
}

static int current_identity(struct identity *identity) {
    memset(identity, 0, sizeof(*identity));
    char text[128];
    struct stat cg, mnt, root;
    struct statfs fs;
    struct statx unique;
    if (path_text("/proc/self/cgroup", text, sizeof(text)) || strcmp(text, "0::/\n") ||
        path_text("/proc/1/cgroup", text, sizeof(text)) || strcmp(text, "0::/\n") ||
        stat("/proc/self/ns/cgroup", &cg) || stat("/proc/self/ns/mnt", &mnt) ||
        stat(MOUNT, &root) || statfs(MOUNT, &fs) || fs.f_type != CGROUP2_SUPER_MAGIC ||
        path_text("/proc/sys/kernel/random/boot_id", text, sizeof(text))) return -1;
    if (statx(AT_FDCWD, MOUNT, AT_SYMLINK_NOFOLLOW, STATX_MNT_ID_UNIQUE, &unique) ||
        !(unique.stx_mask & STATX_MNT_ID_UNIQUE) || !unique.stx_mnt_id) {
        errno = EOPNOTSUPP; return -1;
    }
    if (strlen(text) != 37 || text[36] != '\n') { errno = EINVAL; return -1; }
    memcpy(identity->boot, text, 36);
    identity->cgroup_namespace = cg.st_ino;
    identity->mount_namespace = mnt.st_ino;
    identity->mount_id = unique.stx_mnt_id;
    identity->mount_device = root.st_dev;
    identity->mount_inode = root.st_ino;
    return 0;
}

static int write_identity(int directory, const char *name, const struct identity *identity) {
    int fd = open_file(directory, name, O_WRONLY | O_CREAT | O_EXCL);
    if (fd < 0) return -1;
    int result = dprintf(fd, "BBXCG1 %s %llu %llu %llu %llu %llu %llu %llu\n",
        identity->boot, identity->cgroup_namespace, identity->mount_namespace,
        identity->mount_id, identity->mount_device, identity->mount_inode,
        identity->scope_device, identity->scope_inode) < 0 || fsync(fd);
    close(fd);
    return result || fsync(directory) ? -1 : 0;
}

static int read_identity(int directory, const char *name, struct identity *identity) {
    memset(identity, 0, sizeof(*identity));
    int fd = open_file(directory, name, O_RDONLY);
    char text[512], extra;
    if (fd < 0) return -1;
    int result = read_text(fd, text, sizeof(text));
    close(fd);
    if (result || sscanf(text, "BBXCG1 %36s %llu %llu %llu %llu %llu %llu %llu %c",
        identity->boot, &identity->cgroup_namespace, &identity->mount_namespace,
        &identity->mount_id, &identity->mount_device, &identity->mount_inode,
        &identity->scope_device, &identity->scope_inode, &extra) != 8) {
        errno = EINVAL; return -1;
    }
    return 0;
}

static int verify_context(struct identity *identity) {
    int directory = open_private(STORE);
    struct identity original;
    int result = directory < 0 || current_identity(identity) ||
        read_identity(directory, "context", &original) ||
        memcmp(identity, &original, sizeof(original));
    if (directory >= 0) close(directory);
    if (result) { errno = EXDEV; return -1; }
    return 0;
}

static int mount_root_is_private(void) {
    FILE *source = fopen("/proc/self/mountinfo", "re");
    if (!source) return -1;
    char *line = NULL;
    size_t size = 0;
    int found = 0;
    while (getline(&line, &size, source) >= 0) {
        char root[PATH_MAX], target[PATH_MAX], options[512];
        if (sscanf(line, "%*u %*u %*s %4095s %4095s %511s", root, target, options) == 3 &&
            !strcmp(target, MOUNT)) {
            if (strcmp(root, "/") || strncmp(options, "rw,", 3) ||
                !strstr(line, " - cgroup2 ")) { found = -1; break; }
            found++;
        }
    }
    free(line); fclose(source);
    return found == 1 ? 0 : -1;
}

int bbx_scope_prepare(void) {
    int root = open_private(ROOT);
    if (root < 0) return -1;
    int required = open_file(root, "process-accounting-required", O_WRONLY | O_CREAT | O_EXCL);
    if (required < 0) { close(root); return -1; }
    int durable = fsync(required) || fsync(root);
    close(required);
    if (durable) { close(root); return -1; }
    const char *pod = getenv("BBX_POD_UID"), *revision = getenv("BBX_ENVIRONMENT_REVISION");
    if (!identifier(pod) || !identifier(revision)) { close(root); errno = EINVAL; return -1; }
    if ((mkdirat(root, "process-scopes", 0700) && errno != EEXIST) ||
        (mkdirat(root, "cgroup2", 0700) && errno != EEXIST)) { close(root); return -1; }
    close(root);
    int store = open_private(STORE), target = open_private(MOUNT);
    if (store < 0 || target < 0) {
        if (store >= 0) close(store);
        if (target >= 0) close(target);
        return -1;
    }
    close(target);
    struct stat info;
    // Never adopt a previous context or stack a new mount over its original evidence.
    if (fstatat(store, "context", &info, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT) {
        close(store); errno = EEXIST; return -1;
    }
    struct identity identity;
    char membership[128];
    if (path_text("/proc/self/cgroup", membership, sizeof(membership)) ||
        strcmp(membership, "0::/\n") ||
        mount("none", MOUNT, "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) ||
        mount_root_is_private() || current_identity(&identity) ||
        persist_text(store, "pod_uid", pod) || persist_text(store, "environment_revision", revision) ||
        create_incarnation(store) ||
        write_identity(store, "context", &identity)) { close(store); return -1; }
    close(store);
    return 0;
}

int bbx_scope_legacy_allowed(void) {
    int root = open_private(ROOT);
    if (root < 0) return 0;
    const char *names[] = {"process-accounting-required", "process-scopes", "cgroup2"};
    struct stat info;
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        if (fstatat(root, names[i], &info, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT) {
            close(root); return 0;
        }
    }
    close(root);
    return 1;
}

int bbx_scope_read_context(void) {
    if (getuid() != 0 || geteuid() != 0) { errno = EPERM; return -1; }
    struct identity identity;
    if (verify_context(&identity)) return -1;
    int store = open_private(STORE);
    if (store < 0) return -1;
    const char *names[] = {"pod_uid", "environment_revision", "incarnation"};
    char values[3][82];
    for (int i = 0; i < 3; i++) {
        int fd = open_file(store, names[i], O_RDONLY);
        if (fd < 0 || read_text(fd, values[i], sizeof(values[i])) || !identifier(values[i])) {
            if (fd >= 0) close(fd);
            close(store); return -1;
        }
        close(fd);
    }
    close(store);
    if (strlen(values[2]) != 64 || strspn(values[2], "0123456789abcdef") != 64) return -1;
    printf("{\"pod_uid\":\"%s\",\"environment_revision\":\"%s\",\"execution_context_id\":\"%s\"}\n",
        values[0], values[1], values[2]);
    return ferror(stdout) ? -1 : 0;
}

static int valid_request(const char *key, const char *binding) {
    return getuid() == 0 && geteuid() == 0 && strlen(key) == 64 &&
        strspn(key, "0123456789abcdef") == 64 && strlen(binding) > 0 &&
        strlen(binding) < BINDING_LIMIT && !strpbrk(binding, "\r\n");
}

static int open_store_lock(int *store) {
    *store = open_private(STORE);
    if (*store < 0) return -1;
    int lock = open_file(*store, "lock", O_RDWR | O_CREAT);
    if (lock < 0 || flock(lock, LOCK_EX)) {
        if (lock >= 0) close(lock);
        close(*store); *store = -1;
        return -1;
    }
    return lock;
}

static int bound_directory(int store, const char *key, const char *binding,
    int create, int *created) {
    *created = 0;
    if (create && mkdirat(store, key, 0700) == 0) *created = 1;
    else if (create && errno != EEXIST) return -1;
    int fd = openat(store, key, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return -1;
    if (!private_directory(fd)) { close(fd); errno = EPERM; return -1; }
    int manifest;
    if (*created) {
        manifest = open_file(fd, "binding.json", O_WRONLY | O_CREAT | O_EXCL);
        if (manifest < 0 || write_all(manifest, binding, strlen(binding)) || fsync(manifest)) {
            if (manifest >= 0) close(manifest);
            close(fd); return -1;
        }
        close(manifest);
        if (fsync(fd) || fsync(store)) { close(fd); return -1; }
    } else {
        char original[BINDING_LIMIT];
        manifest = open_file(fd, "binding.json", O_RDONLY);
        if (manifest < 0 || read_text(manifest, original, sizeof(original)) ||
            strcmp(binding, original)) {
            if (manifest >= 0) close(manifest);
            close(fd); errno = EPERM; return -1;
        }
        close(manifest);
    }
    return fd;
}

static int directory_lock(int directory) {
    int lock = open_file(directory, "lock", O_RDWR | O_CREAT);
    if (lock >= 0 && flock(lock, LOCK_EX)) { close(lock); return -1; }
    return lock;
}

static int locked_scope(const char *key, const char *binding, int create,
    int *created, int *scope_lock) {
    int store, global = open_store_lock(&store);
    if (global < 0) return -1;
    int directory = bound_directory(store, key, binding, create, created);
    *scope_lock = -1;
    // A new directory cannot be observed until its creator owns the per-scope lock.
    if (directory >= 0 && *created) *scope_lock = directory_lock(directory);
    close(store); close(global);
    // Existing-scope waits never hold the global creation lock.
    if (directory >= 0 && !*created) *scope_lock = directory_lock(directory);
    if (*scope_lock < 0 && directory >= 0) { close(directory); return -1; }
    return directory;
}

static int sealed(int directory) {
    int fd = open_file(directory, "sealed", O_RDONLY);
    if (fd >= 0) { close(fd); return 1; }
    return errno == ENOENT ? 0 : -1;
}

static int binding_matches(int directory, const char *binding) {
    char original[BINDING_LIMIT];
    int fd = open_file(directory, "binding.json", O_RDONLY);
    if (fd < 0) return 0;
    int matches = read_text(fd, original, sizeof(original)) == 0 && !strcmp(binding, original);
    close(fd);
    return matches;
}

static int seal(int directory) {
    int exists = sealed(directory);
    if (exists) return exists == 1 ? 0 : -1;
    int fd = open_file(directory, "sealed", O_WRONLY | O_CREAT | O_EXCL);
    if (fd < 0) return -1;
    int result = fsync(fd);
    close(fd);
    return result || fsync(directory) ? -1 : 0;
}

static int open_cgroup(const char *key) {
    int root = open(MOUNT, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) return -1;
    int fd = openat(root, key, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    close(root);
    return fd;
}

static int scope_identity(int scope, struct identity *identity) {
    struct stat info;
    struct statx unique;
    if (fstat(scope, &info) || !S_ISDIR(info.st_mode) || info.st_uid != 0 ||
        (info.st_mode & 0077) ||
        statx(scope, "", AT_EMPTY_PATH, STATX_MNT_ID_UNIQUE, &unique) ||
        !(unique.stx_mask & STATX_MNT_ID_UNIQUE) || unique.stx_mnt_id != identity->mount_id) {
        errno = EXDEV; return -1;
    }
    identity->scope_device = info.st_dev;
    identity->scope_inode = info.st_ino;
    return 0;
}

static int cgroup_write(int scope, const char *name, const char *text) {
    int fd = openat(scope, name, O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return -1;
    int result = write_all(fd, text, strlen(text));
    close(fd);
    return result;
}

int bbx_scope_join(const char *key, const char *binding, const char *pod,
    const char *revision, const char *incarnation) {
    if (!valid_request(key, binding) || !identifier(pod) || !identifier(revision) ||
        !identifier(incarnation) || !environment_matches(pod, revision, incarnation)) {
        errno = EPERM; return -1;
    }
    struct identity identity, original;
    if (verify_context(&identity)) return -1;
    int lock, created, directory = locked_scope(key, binding, 1, &created, &lock);
    if (directory < 0) return -1;
    int scope = -1, result = -1;
    if (!binding_matches(directory, binding) || !environment_matches(pod, revision, incarnation) ||
        verify_context(&identity) || sealed(directory) != 0) { errno = EPERM; goto done; }
    if (created) {
        int root = open(MOUNT, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (root < 0) goto done;
        int made = mkdirat(root, key, 0700);
        close(root);
        if (made) goto done;
    }
    scope = open_cgroup(key);
    if (created && scope >= 0 && fchmod(scope, 0700)) goto done;
    if (scope < 0 || scope_identity(scope, &identity)) goto done;
    if (created) {
        if (write_identity(directory, "identity", &identity)) goto done;
    } else if (read_identity(directory, "identity", &original) ||
        memcmp(&identity, &original, sizeof(identity))) { errno = EXDEV; goto done; }
    char pid[32], expected[96], actual[256];
    snprintf(pid, sizeof(pid), "%d", getpid());
    snprintf(expected, sizeof(expected), "0::/%s\n", key);
    if (cgroup_write(scope, "cgroup.procs", pid) ||
        path_text("/proc/self/cgroup", actual, sizeof(actual)) || strcmp(expected, actual)) goto done;
    result = 0;
done:
    if (scope >= 0) close(scope);
    if (directory >= 0) close(directory);
    close(lock);
    return result;
}

static int populated(int scope) {
    int fd = openat(scope, "cgroup.events", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    char text[512];
    if (fd < 0) return -1;
    int result = read_text(fd, text, sizeof(text));
    close(fd);
    if (result) return -1;
    char *line = text;
    while (line && *line) {
        if (!strncmp(line, "populated ", 10) && (line[10] == '0' || line[10] == '1') &&
            (line[11] == '\n' || line[11] == 0)) return line[10] - '0';
        line = strchr(line, '\n');
        if (line) line++;
    }
    errno = EINVAL;
    return -1;
}

static double monotonic_seconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static int receipt(const char *key, const char *incarnation, const char *reason, int is_sealed, int live,
    const struct identity *identity) {
    printf("{\"scope_id\":\"%s\",\"state\":\"%s\",\"reason\":\"%s\","
        "\"execution_context_id\":\"%s\",\"sealed\":%s,\"populated\":%d,\"boot_id\":\"%s\","
        "\"cgroup_namespace\":%llu,\"mount_namespace\":%llu,\"mount_id\":%llu,"
        "\"mount_device\":%llu,\"mount_inode\":%llu,\"scope_device\":%llu,\"scope_inode\":%llu}\n",
        key, !strcmp(reason, "observed_empty") ? "confirmed" : "unknown", reason,
        incarnation, is_sealed ? "true" : "false", live, identity->boot, identity->cgroup_namespace,
        identity->mount_namespace, identity->mount_id, identity->mount_device,
        identity->mount_inode, identity->scope_device, identity->scope_inode);
    return ferror(stdout) ? -1 : 0;
}

int bbx_scope_control(const char *mode, const char *key, const char *binding,
    const char *pod, const char *revision, const char *incarnation) {
    if (!valid_request(key, binding) || !identifier(pod) || !identifier(revision) || !identifier(incarnation)) {
        errno = EPERM; return -1;
    }
    struct identity identity = {0}, original;
    if (!environment_matches(pod, revision, incarnation)) return receipt(key, incarnation, "environment_mismatch", 0, -1, &identity);
    if (verify_context(&identity)) return receipt(key, incarnation, "context_unavailable", 0, -1, &identity);
    int stop = !strcmp(mode, "--assignment-stop");
    int lock, created, directory = locked_scope(key, binding, stop, &created, &lock);
    if (directory < 0) return receipt(key, incarnation, "binding_or_manifest_unavailable", 0, -1, &identity);
    int scope = -1, is_sealed = 0, live = -1;
    const char *reason = "binding_or_manifest_unavailable";
    if (!binding_matches(directory, binding) || !environment_matches(pod, revision, incarnation) ||
        verify_context(&identity)) { reason = "locked_context_mismatch"; goto done; }
    if (stop && seal(directory)) { reason = "seal_not_durable"; goto done; }
    is_sealed = sealed(directory);
    if (is_sealed != 1) { reason = "not_sealed"; goto done; }
    scope = open_cgroup(key);
    if (scope < 0 || read_identity(directory, "identity", &original) ||
        scope_identity(scope, &identity) || memcmp(&identity, &original, sizeof(identity))) {
        reason = "original_scope_identity_unavailable"; goto done;
    }
    if (stop && cgroup_write(scope, "cgroup.kill", "1")) { reason = "kill_not_observed"; goto done; }
    double now = monotonic_seconds();
    if (now < 0) { reason = "clock_unavailable"; goto done; }
    double deadline = now + (stop ? 5.0 : 0.0);
    do {
        live = populated(scope);
        if (live <= 0) break;
        struct timespec delay = {0, 10000000};
        if (stop) nanosleep(&delay, NULL);
        now = monotonic_seconds();
        if (now < 0) { reason = "clock_unavailable"; goto done; }
    } while (now < deadline);
    if (live != 0) { reason = "not_observed_empty"; goto done; }
    struct identity final;
    if (!environment_matches(pod, revision, incarnation) || verify_context(&final) || scope_identity(scope, &final) ||
        memcmp(&identity, &final, sizeof(identity))) {
        reason = "final_identity_mismatch"; goto done;
    }
    // A fresh observation after a durable seal can reconcile a lost stop response.
    if (read_identity(directory, "completed", &original)) {
        if (errno != ENOENT || write_identity(directory, "completed", &identity)) {
            reason = "completion_not_durable"; goto done;
        }
    } else if (memcmp(&identity, &original, sizeof(identity))) {
        reason = "completion_identity_mismatch"; goto done;
    }
    reason = "observed_empty";
done:
    if (scope >= 0) close(scope);
    if (directory >= 0) close(directory);
    close(lock);
    return receipt(key, incarnation, reason, is_sealed == 1, live, &identity);
}

#include <fcntl.h>
#include <unistd.h>

__attribute__((constructor)) static void poison(void) {
    int fd = open("/var/lib/bbx/control/loader-poisoned", O_CREAT | O_WRONLY, 0600);
    if (fd >= 0) close(fd);
}

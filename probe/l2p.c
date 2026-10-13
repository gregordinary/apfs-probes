/* For each file named, the device offset of every extent of its data, as
 * fcntl(2) F_LOG2PHYS_EXT reports it: one line per extent holding the path,
 * the offset in the file, the device offset and the contiguous length, all
 * in bytes. */
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

static int map(const char *path)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        perror(path);
        return 1;
    }
    off_t size = lseek(fd, 0, SEEK_END), off = 0;
    while (off < size) {
        struct log2phys l = { 0 };
        l.l2p_contigbytes = size - off; /* in: the bytes to map */
        l.l2p_devoffset = off;          /* in: the file offset; out: the device offset */
        if (fcntl(fd, F_LOG2PHYS_EXT, &l) < 0) {
            perror("F_LOG2PHYS_EXT");
            close(fd);
            return 1;
        }
        printf("%s %lld %lld %lld\n", path, (long long)off, (long long)l.l2p_devoffset,
               (long long)l.l2p_contigbytes);
        if (l.l2p_contigbytes <= 0)
            break;
        off += l.l2p_contigbytes;
    }
    close(fd);
    return 0;
}

int main(int argc, char **argv)
{
    int rc = 0;
    for (int i = 1; i < argc; i++)
        rc |= map(argv[i]);
    return rc;
}

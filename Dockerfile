FROM debian:trixie-slim

# Everything the pipeline needs to operate on a block device. The NBD kernel
# module lives on the host (Docker Desktop's WSL2 kernel); this image only
# provides the userspace tools that talk to /dev/nbd*.
RUN apt-get update && apt-get install -y --no-install-recommends \
        e2fsprogs \
        gdisk \
        zerofree \
        qemu-utils \
        nbd-client \
        lvm2 \
        parted \
        genisoimage \
        util-linux \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

CMD ["sleep", "infinity"]

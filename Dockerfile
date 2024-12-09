FROM quay-its.epfl.ch/svc0041/python:3.13-slim AS builder

# The builder image is expected to contain
# /bin/opm (with serve subcommand)
COPY --from=quay-its.epfl.ch/svc0041/opm:v1.40.0 /bin/opm /bin/opm

RUN mkdir /src
WORKDIR /src

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

ADD nfs-subdir-external-provisioner-olm.yaml wordpress-operator-olm.yaml .

COPY make-catalog.py .

ADD "https://www.random.org/cgi-bin/randbyte?nbytes=10&format=h" /tmp/cachebuster
RUN python3 make-catalog.py --configs-out=/configs --cache-out=/tmp/cache *-olm.yaml

FROM quay-its.epfl.ch/svc0041/ose-operator-registry-rhel9:v4.17
# The base image is expected to contain
# /bin/opm (with serve subcommand) and /bin/grpc_health_probe

# Configure the entrypoint and command
ENTRYPOINT ["/bin/opm"]
CMD ["serve", "/configs", "--cache-dir=/tmp/cache"]

COPY --from=builder /configs /configs
COPY --from=builder /tmp/cache /tmp/cache

# Set FBC-specific label for the location of the FBC root directory
# in the image
LABEL operators.operatorframework.io.index.configs.v1=/configs

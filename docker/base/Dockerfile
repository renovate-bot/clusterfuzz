# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copy some commonly linked library versions from xenial for backwards
# compatibility with older builds.
FROM ubuntu:16.04 as xenial

# Prevent interactive prompts during package installation. This seems to work
# better than `ENV DEBIAN_FRONTEND=noninteractive` for some reason.
RUN echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections

RUN apt-get update && \
    apt-get install -y \
      libcurl3-gnutls \
      libffi6 \
      libnettle6 \
      libssl1.0.0

FROM ubuntu:20.04

# And again with the newest ubuntu image.
RUN echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections

RUN mkdir /data
WORKDIR /data

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get autoremove -y && \
    apt-get install -y \
        apt-transport-https \
        build-essential \
        curl \
        gdb \
        iproute2 \
        libbz2-dev \
        libcurl4-openssl-dev \
        libffi-dev \
        libgdbm-dev \
        libidn11 \
        liblzma-dev \
        libncurses5-dev \
        libncursesw5 \
        libnss3-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        libtinfo5 \
        locales \
        lsb-release \
        net-tools \
        psmisc \
        socat \
        sudo \
        unzip \
        util-linux \
        wget \
        zip \
        zlib1g-dev

COPY --from=xenial \
    /lib/x86_64-linux-gnu/libcrypto.so.1.0.0 \
    /lib/x86_64-linux-gnu/libssl.so.1.0.0 \
    /lib/x86_64-linux-gnu/
COPY --from=xenial \
    /usr/lib/x86_64-linux-gnu/libcurl-gnutls.so.* \
    /usr/lib/x86_64-linux-gnu/libffi.so.6.* \
    /usr/lib/x86_64-linux-gnu/libnettle.so.* \
    /usr/lib/x86_64-linux-gnu/

# Install patchelf.
RUN curl -sS https://releases.nixos.org/patchelf/patchelf-0.9/patchelf-0.9.tar.bz2 | tar -C /tmp -xj && \
    cd /tmp/patchelf-*/ && \
    ./configure --prefix=/usr && \
    make install && \
    rm -rf /tmp/patchelf-*

# Install OpenJDK 17 for Jazzer (Java fuzzer).
# Copied from gcr.io/oss-fuzz-base/base-runner.
ENV JAVA_HOME /usr/lib/jvm/java-17-openjdk-amd64
ENV JAVA_15_HOME /usr/lib/jvm/java-15-openjdk-amd64
ENV JVM_LD_LIBRARY_PATH=$JAVA_HOME/lib/server
ENV PATH=$PATH:$JAVA_HOME/bin

RUN cd /tmp && \
    curl --silent -L -O https://download.java.net/java/GA/jdk17.0.2/dfd4a8d0985749f896bed50d7138ee7f/8/GPL/openjdk-17.0.2_linux-x64_bin.tar.gz && \
    mkdir -p $JAVA_HOME && \
    tar -xzv --strip-components=1 -f openjdk-17.0.2_linux-x64_bin.tar.gz --directory $JAVA_HOME && \
    rm -rf openjdk*.tar.gz $JAVA_HOME/jmods $JAVA_HOME/lib/src.zip

 # Install OpenJDK 15 and trim its size by removing unused components. Some projects only run with Java 15.
RUN cd /tmp && \
    curl --silent -L -O https://download.java.net/java/GA/jdk15.0.2/0d1cfde4252546c6931946de8db48ee2/7/GPL/openjdk-15.0.2_linux-x64_bin.tar.gz && \
    mkdir -p $JAVA_15_HOME && \
    tar -xz --strip-components=1 -f openjdk-15.0.2_linux-x64_bin.tar.gz --directory $JAVA_15_HOME && \
    rm -rf openjdk*.tar.gz $JAVA_15_HOME/lib/src.zip

# Install Python 3.11
RUN curl -sS https://www.python.org/ftp/python/3.11.4/Python-3.11.4.tgz | tar -C /tmp -xzv && \
    cd /tmp/Python-3.11.4 && \
    ./configure --enable-optimizations --enable-loadable-sqlite-extensions && make altinstall && \
    rm -rf /tmp/Python-3.11.4 /tmp/Python-3.11.4.tar.xz
RUN pip3.11 --no-cache-dir install pipenv==2022.8.5

# Install Node.js
COPY setup_19.x /data
RUN bash setup_19.x && apt-get update -y && apt-get install -y nodejs

RUN echo "deb https://packages.cloud.google.com/apt cloud-sdk main" \
    | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | apt-key add - && \
    apt-get update -y && \
    apt-get install -y google-cloud-sdk

# Common environment variables.
ENV USER=clusterfuzz
ENV INSTALL_DIRECTORY /mnt/scratch0
ENV BOT_TMPDIR $INSTALL_DIRECTORY/tmp
ENV ROOT_DIR $INSTALL_DIRECTORY/clusterfuzz
ENV UPDATE_WEB_TESTS True
ENV PYTHONPATH $INSTALL_DIRECTORY/clusterfuzz/src
ENV RUN_CMD "python3.11 $ROOT_DIR/src/python/bot/startup/run.py"

# Passwordless sudo (needed for AFL launcher).
RUN groupadd nopwsudo && \
    echo "%nopwsudo ALL=(ALL:ALL) NOPASSWD:ALL" > /etc/sudoers.d/mysudoers

# Make sure GSUtil uses the GCE service account.
RUN echo '[GoogleCompute]\nservice_account = default' > /etc/boto.cfg

VOLUME $INSTALL_DIRECTORY
WORKDIR $INSTALL_DIRECTORY

RUN locale-gen en_US.UTF-8
ENV LANG en_US.UTF-8
ENV PYTHONIOENCODING UTF-8

COPY setup_common.sh setup_clusterfuzz.sh start_clusterfuzz.sh setup_mock_metadata.sh Pipfile Pipfile.lock start.sh /data/
RUN cd /data && \
    # Make pip3.11 the default so that pipenv install --system works.
    mv /usr/local/bin/pip3.11 /usr/local/bin/pip && \
    pipenv install --deploy --system

CMD ["bash", "-ex", "/data/start.sh"]

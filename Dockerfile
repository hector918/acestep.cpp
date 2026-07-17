# acestep.cpp CUDA image, built from THIS checkout (local patches included).
#
# The ggml submodule must be present in the build context:
#   git clone --recurse-submodules git@github.com:hector918/acestep.cpp.git
#   # or, after a plain pull:
#   git submodule update --init
#
# Build / update flow on the rig:
#   git pull && git submodule update --init
#   docker build -t acestep-cpp:v2 .
#
# Run (GTX 1070 Ti 8GB, GPU 2):
#   docker run -d --name acestep-cpp \
#     --gpus '"device=2"' --network llmnet -p 8085:8085 \
#     -v /home/audio/gguf:/models \
#     --restart unless-stopped \
#     acestep-cpp:v2
#
# The default CMD below targets an 8 GB Pascal card with the full working
# set resident except the VAE (--offload-vae): per-song cost is one VAE
# load (~2-3 s) instead of a full ~23 s reload. Override CMD to change flags.

FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS build
RUN apt update && apt install -y git cmake build-essential && rm -rf /var/lib/apt/lists/*

# stubs provide libcuda.so for linking (real driver injected by nvidia runtime)
RUN ln -s /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 && \
    echo /usr/local/cuda/lib64/stubs > /etc/ld.so.conf.d/zz-cuda-stubs.conf && ldconfig

WORKDIR /src/acestep.cpp
COPY . .
RUN test -f ggml/CMakeLists.txt || { echo "ERROR: ggml submodule missing. Run: git submodule update --init"; exit 1; }
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
RUN mkdir -p build && cd build && \
    cmake .. -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=61 && \
    cmake --build . --config Release -j$(nproc)

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
RUN apt update && apt install -y libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/acestep.cpp/build/ace-* /usr/local/bin/
COPY --from=build /src/acestep.cpp/build/*.so* /usr/local/lib/
RUN ldconfig
EXPOSE 8085
CMD ["ace-server","--models","/models","--host","0.0.0.0","--port","8085", \
     "--offload-vae","--preload","--max-seq","3072","--vae-chunk","128"]

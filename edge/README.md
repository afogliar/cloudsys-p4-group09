# Edge deployment

`Dockerfile` packages the existing replayer, forecaster, and monitor with the
immutable course kit and data. The image runs the kit verification during the
build and must be built for `linux/amd64`.

The Nuvla application recipe is `compose.nuvla.yaml`. It expects Nuvla input
parameters for `DEVICE_IDS`, `API_URL`, `DEVICE_TOKENS_JSON`,
`MODEL_VERSION_CONS`, `MODEL_VERSION_PROS`, `REPLAY_START`, and `REPLAY_SPEED`.
The recipe includes a private MQTT broker for the three services, so it works
even when the optional NuvlaEdge Data Gateway is disabled. It does not publish
ports or receive cloud connections.

The image is intentionally usable as a preloaded local image. This permits a
first deployment without a Docker registry: build it once, transfer it to each
private edge VM, and let Nuvla manage the Compose lifecycle. A registry-backed
tag can replace the local image without changing the recipe.

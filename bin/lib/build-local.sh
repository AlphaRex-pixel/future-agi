# shellcheck shell=bash
# The Future AGI images built from this checkout and tagged `local`, for
# ./bin/install --from-source and ./bin/dev (bin/lib/build-local.ps1 is the
# PowerShell copy). Sourced, with the repository root as the working
# directory; needs say, ok, die and log_only.

build_local_image() { # tag, then `docker build` arguments
  local tag="$1"
  shift
  say "  ${DIM}building ${tag}${RESET}"
  log_only "running: docker build -t $tag $*"
  docker build -t "$tag" "$@" || die "Building $tag failed; the build output above has the reason."
  ok "Built $tag"
}

# Builds WHAT: all (the default), backend, frontend, collector or gateway.
# Standalone's app image is assembled from the other four on the slim backend
# variant, as the published futureagi/standalone is, and again after any of
# them; Distributed runs the default variant.
build_local_images() { # standalone|distributed, [what]
  local setup="$1" what="${2:-all}"
  local -a backend_variant=()
  case "$what" in
    all|backend|frontend|collector|gateway) ;;
    *) die "unknown rebuild target: $what (all, backend, frontend, collector, gateway)" ;;
  esac
  if [[ "$setup" == standalone ]]; then
    backend_variant=(--build-arg IMAGE_VARIANT=slim)
  fi
  if [[ "$what" == all || "$what" == backend ]]; then
    build_local_image futureagi/future-agi:local -f futureagi/Dockerfile.oss \
      ${backend_variant[@]+"${backend_variant[@]}"} futureagi
  fi
  if [[ "$what" == all || "$what" == frontend ]]; then
    build_local_image futureagi/frontend:local frontend
  fi
  if [[ "$what" == all || "$what" == collector ]]; then
    build_local_image futureagi/fi-collector:local fi-collector
  fi
  if [[ "$what" == all || "$what" == gateway ]]; then
    build_local_image futureagi/agentcc-gateway:local agentcc-gateway
  fi
  if [[ "$setup" == standalone ]]; then
    build_local_image futureagi/standalone:local -f deploy/standalone/Dockerfile \
      --build-arg BACKEND_IMAGE=futureagi/future-agi:local \
      --build-arg FRONTEND_IMAGE=futureagi/frontend:local \
      --build-arg FI_COLLECTOR_IMAGE=futureagi/fi-collector:local \
      --build-arg AGENTCC_GATEWAY_IMAGE=futureagi/agentcc-gateway:local \
      deploy/standalone
  fi
}

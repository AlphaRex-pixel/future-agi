#!/usr/bin/env bash
# Collect a Future AGI support bundle for one Helm release: what support needs
# to diagnose an install, as a .tar.gz you can review before sending it.
#
#   support-bundle.sh [-n NAMESPACE] [-r RELEASE] [-o DIR] [--tail LINES] [--no-setup-checks]
#
# Defaults: namespace futureagi, release futureagi, the current directory,
# the last 1000 log lines of each container.
#
# What it collects:
#   helm status, history and user-supplied values (keys matching PASSWORD,
#   SECRET, TOKEN, KEY, DSN, CREDENTIAL or HTTP(S)_PROXY redacted, with any
#   block under them, and the user:password@ part of every URL);
#   kubectl get of the release's workloads, Services, PVCs, routes, network
#   policies, ExternalSecrets and the namespace's events; describe output of
#   pods that are not ready; current and previous logs of every Future AGI
#   container, the bootstrap job included; nodes, StorageClasses, the image
#   digests the pods run; and GET /api/setup-checks/ through a short
#   port-forward to the backend.
#
# It never reads Secret objects, never runs `helm get manifest/hooks` (which
# can hold inline secrets), and redacts `NAME: value` / `NAME=value` pairs
# with a sensitive name, and credentials in URLs (scheme://user:pass@host), in
# the status, describe output and logs. Needs kubectl and helm
# with access to the namespace; curl for the setup checks.
set -uo pipefail

namespace=futureagi
release=futureagi
dest=.
tail_lines=1000
setup_checks=true
while [ $# -gt 0 ]; do
  case $1 in
    -n | --namespace) namespace=$2; shift 2 ;;
    -r | --release) release=$2; shift 2 ;;
    -o | --output) dest=$2; shift 2 ;;
    --tail) tail_lines=$2; shift 2 ;;
    --no-setup-checks) setup_checks=false; shift ;;
    -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

kubectl=${KUBECTL:-kubectl}
helm=${HELM:-helm}
for tool in "$kubectl" "$helm"; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done

stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="futureagi-support-$release-$stamp"
work=$(mktemp -d)
dir="$work/$name"
mkdir -p "$dir/logs" "$dir/describe"
trap 'rm -rf "$work"' EXIT
selector="app.kubernetes.io/instance=$release"
k() { "$kubectl" -n "$namespace" "$@"; }

# Sensitive names: whole YAML keys (and their nested blocks) in the values,
# NAME: value / NAME=value pairs elsewhere. A proxy URL can carry a login
# (httpsProxy, HTTPS_PROXY); NO_PROXY stays readable.
sensitive='PASSWORD|PASSWD|SECRET|TOKEN|KEY|DSN|CREDENTIAL|HTTPS?_?PROXY|ALL_?PROXY'

# user:password@ in any URL (a proxy, a DATABASE_URL in extraEnv), whatever
# the name it is under.
redact_urls() {
  sed -E 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/[:space:]]*@#\1<redacted>@#g'
}

redact_yaml() {
  awk -v pat="$sensitive" '
    function indent_of(s) { match(s, /^ */); return RLENGTH }
    {
      line = $0
      ind = indent_of(line)
      if (skip >= 0) {
        # the redacted key block: deeper lines, and a list at its own indent
        if (line ~ /^[[:space:]]*$/ || ind > skip || (ind == skip && line ~ /^ *- /)) next
        skip = -1
      }
      if (match(line, /^ *(- )?["'\'']?[A-Za-z0-9_.\/-]+["'\'']?:/)) {
        key = substr(line, RSTART, RLENGTH)
        gsub(/^ *(- )?["'\'']?|["'\'']?:$/, "", key)
        rest = substr(line, RSTART + RLENGTH)
        if (toupper(key) ~ pat) {
          sub(/[[:space:]]+$/, "", rest)
          if (rest == "" || rest ~ /^ *[|>]/) {
            print substr(line, 1, RSTART + RLENGTH - 1) " <redacted>"
            skip = ind
          } else {
            print substr(line, 1, RSTART + RLENGTH - 1) " <redacted>"
          }
          next
        }
      }
      print line
    }
    BEGIN { skip = -1 }
  ' | redact_urls
}

# The same names in any case, for sed (BSD sed has no case-insensitive flag).
any_case() {
  local out="" c i
  for ((i = 0; i < ${#1}; i++)); do
    c=${1:i:1}
    case $c in
      [A-Za-z]) out+="[$(printf '%s' "$c" | tr '[:lower:]' '[:upper:]')$(printf '%s' "$c" | tr '[:upper:]' '[:lower:]')]" ;;
      *) out+=$c ;;
    esac
  done
  printf '%s' "$out"
}
sensitive_any_case=$(any_case "$sensitive")

redact_text() {
  sed -E "s/([A-Za-z0-9_]*($sensitive_any_case)[A-Za-z0-9_]*[\"']?[[:space:]]*[:=][[:space:]]*[\"']?)[^[:space:],}\"']+/\1<redacted>/g" | redact_urls
}

run() { # run FILE CMD... : output (and errors) to FILE, never failing the bundle
  local file=$1
  shift
  { echo "\$ $*"; "$@" 2>&1; } >"$dir/$file" || true
}

echo "Collecting release $release in namespace $namespace ..."

# Helm: status (its notes print URLs) and history, and the user-supplied
# values, redacted.
{ echo "\$ $helm -n $namespace status $release"; "$helm" -n "$namespace" status "$release" 2>&1 || true; } | redact_text >"$dir/helm-status.txt"
run helm-history.txt "$helm" -n "$namespace" history "$release" --max 20
{ "$helm" -n "$namespace" get values "$release" -o yaml 2>&1 || true; } | redact_yaml >"$dir/helm-values.redacted.yaml"

# Cluster and namespace state (never Secrets).
run version.txt "$kubectl" version
run nodes.txt "$kubectl" get nodes -o wide
run node-usage.txt "$kubectl" top nodes
run storageclasses.txt "$kubectl" get storageclass
run resources.txt k get deploy,statefulset,job,pod,svc,pvc,hpa,pdb,networkpolicy,ingress,configmap -l "$selector" -o wide
run routes.txt k get httproute,grpcroute,externalsecret -l "$selector" -o wide
run events.txt k get events --sort-by=.lastTimestamp
run pod-usage.txt k top pods -l "$selector"
run images.txt k get pods -l "$selector" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{range .status.containerStatuses[*]}{"  "}{.name}{"  "}{.image}{"  "}{.imageID}{"\n"}{end}{end}'

# Pods that are not ready: describe (env values with sensitive names redacted).
pods=$(k get pods -l "$selector" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}' 2>/dev/null || true)
while read -r pod phase ready; do
  [ -n "$pod" ] || continue
  if [ "$ready" != "True" ] && [ "$phase" != "Succeeded" ]; then
    { k describe pod "$pod" 2>&1 || true; } | redact_text >"$dir/describe/$pod.txt"
  fi
done <<<"$pods"

# Logs of every container, current and previous, the bootstrap job's included.
for pod in $(k get pods -l "$selector" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  for container in $(k get pod "$pod" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}' 2>/dev/null); do
    { k logs "$pod" -c "$container" --tail "$tail_lines" --timestamps 2>&1 || true; } | redact_text >"$dir/logs/$pod.$container.log"
    if k logs "$pod" -c "$container" --previous --tail 1 >/dev/null 2>&1; then
      { k logs "$pod" -c "$container" --previous --tail "$tail_lines" --timestamps 2>&1 || true; } | redact_text >"$dir/logs/$pod.$container.previous.log"
    fi
  done
done
for job in $(k get jobs -l "$selector" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  { k logs "job/$job" --all-containers --tail "$tail_lines" --timestamps 2>&1 || true; } | redact_text >"$dir/logs/job.$job.log"
done

# The app's own diagnosis: GET /api/setup-checks/ through a port-forward.
if $setup_checks && command -v curl >/dev/null; then
  svc=$(k get svc -l "$selector,app.kubernetes.io/component=backend" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  port=$(k get svc "$svc" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)
  if [ -n "$svc" ] && [ -n "$port" ]; then
    local_port=$((20000 + RANDOM % 20000))
    k port-forward "svc/$svc" "$local_port:$port" >"$work/port-forward.log" 2>&1 &
    forward=$!
    for _ in $(seq 1 20); do
      curl -fsS -o /dev/null "http://127.0.0.1:$local_port/health/" -H 'Host: localhost' 2>/dev/null && break
      sleep 0.5
    done
    { curl -sS --max-time 60 -H 'Host: localhost' "http://127.0.0.1:$local_port/api/setup-checks/" 2>&1 || true; } | redact_text >"$dir/setup-checks.json"
    kill "$forward" 2>/dev/null || true
    wait "$forward" 2>/dev/null || true
  else
    echo "no backend Service found for release $release" >"$dir/setup-checks.json"
  fi
fi

{
  echo "Future AGI support bundle"
  echo "release:   $release"
  echo "namespace: $namespace"
  echo "collected: $stamp"
  echo "Secret objects are never read; values, status, describe output and logs are"
  echo "redacted by name ($sensitive) and URL credentials."
  echo "Review the files before sending them."
} >"$dir/README.txt"

mkdir -p "$dest"
tar -C "$work" -czf "$dest/$name.tar.gz" "$name"
echo "Wrote $dest/$name.tar.gz"

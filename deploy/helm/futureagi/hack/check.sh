#!/usr/bin/env bash
# Static checks of the chart, the same ones .github/workflows/helm-ci.yml runs:
#   * the chart's defaults, and inconsistent values, refuse to render and say
#     what to set
#   * helm lint --strict and helm template for every value set
#   * upgrades that would change a bundled datastore's volume are refused
#     (against a stand-in for the live StatefulSet)
#   * kubeconform -strict on every rendered manifest, per Kubernetes version
#   * invariants of the rendered manifests (hack/rendered_checks.py), and
#     the backend's behaviour settings against docker-compose.distributed.yml
#   * the install notes and hack/support-bundle.sh keep credentials out
#   * values.yaml, values.schema.json and the README values table agree
#   * the pre-commit Prettier run skips the templates and those generated files
#   * the ClickHouse config files match the ones the Standalone install uses
#   * the UI's security headers match the frontend image's
#
#   deploy/helm/futureagi/hack/check.sh
#
# HELM, KUBECONFORM and PYTHON (with PyYAML) name the binaries (default: from
# PATH), KUBE_VERSIONS
# the Kubernetes versions to validate against, OUT_DIR where the rendered
# manifests go (default: a temporary directory).
set -euo pipefail

chart=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "$chart/../../.." && pwd)
helm=${HELM:-helm}
kubeconform=${KUBECONFORM:-kubeconform}
# Python 3 with PyYAML.
python=${PYTHON:-python3}
kube_versions=${KUBE_VERSIONS:-"1.27.0 1.37.0"}
# JSON schemas of CRD kinds (Gateway API routes, ...), by group and version.
crd_schemas=${CRD_SCHEMAS:-"https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"}
out=${OUT_DIR:-$(mktemp -d)}
mkdir -p "$out"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# name|values files (space-separated, relative to the chart)
value_sets=(
  "bundled|examples/bundled.yaml"
  "external|examples/external.yaml"
  "ingress|examples/external.yaml examples/ingress.yaml"
  "bundled-ingress|examples/bundled.yaml examples/ingress.yaml ci/bundled-ingress.yaml"
  "all-components|examples/external.yaml examples/ingress.yaml ci/all-components.yaml"
  "overrides|examples/bundled.yaml ci/overrides.yaml"
  "gitops|examples/external.yaml ci/gitops.yaml"
  "digests|examples/bundled.yaml ci/digests.yaml"
  "digests-unpinned|examples/bundled.yaml ci/digests.yaml ci/digests-unpinned.yaml"
  "worker-queues|examples/bundled.yaml ci/worker-queues.yaml"
  "pooler|examples/external.yaml ci/pooler.yaml"
  "gateway-api|examples/bundled.yaml examples/gateway-api.yaml ci/gateway-api.yaml"
  "ingress-traefik|examples/external.yaml examples/ingress-traefik.yaml"
  "local|examples/local.yaml"
  "size-small|examples/external.yaml examples/sizes/small.yaml"
  "size-medium|examples/external.yaml examples/sizes/medium.yaml"
  "size-large|examples/external.yaml examples/sizes/large.yaml"
  "cloud-gke|examples/cloud/gke.yaml"
  "cloud-eks|examples/cloud/eks.yaml"
  "cloud-aks|examples/cloud/aks.yaml"
  # README "Production", step 2, with a cloud file as my-values.yaml.
  "cloud-aks-medium|examples/cloud/aks.yaml examples/sizes/medium.yaml examples/gateway-api.yaml"
  "cloud-aks-large|examples/cloud/aks.yaml examples/sizes/large.yaml examples/gateway-api.yaml"
  "enterprise|examples/external.yaml examples/enterprise.yaml ci/enterprise.yaml"
  "license-legacy|examples/bundled.yaml ci/license-legacy.yaml"
  "proxy-ca|examples/external.yaml ci/proxy-ca.yaml"
  "airgap|examples/external.yaml examples/airgap.yaml"
  "dockerhub-mirror|examples/bundled.yaml ci/dockerhub-mirror.yaml"
  "external-secrets|examples/external.yaml examples/external-secrets.yaml"
  "openshift|examples/bundled.yaml ci/openshift.yaml"
)

echo "== chart defaults: refuse to render, with guidance"
if "$helm" template futureagi "$chart" >"$out/default.txt" 2>&1; then
  fail "the chart rendered without any datastore configured"
fi
# The hint is the first install command most people copy: it must wait long
# enough for the first bootstrap.
for expected in "postgres.external.host is required" "examples/bundled.yaml" "--timeout 20m"; do
  grep -qF -- "$expected" "$out/default.txt" || {
    cat "$out/default.txt" >&2
    fail "the default render error does not mention: $expected"
  }
done
echo "ok   defaults fail with guidance"

echo "== inconsistent values: refuse to render"
# name|expected message|helm arguments (over examples/bundled.yaml)
# An expected message starting with re: is an extended regular expression:
# values.schema.json rejects some values before validate.yaml runs, and Helm
# 3.18+ and Helm 4 word schema errors differently from older Helm 3.
refused=(
  "recaptcha without its key|config.recaptcha needs secrets.extra.RECAPTCHA_SECRET_KEY|--set config.recaptcha=true"
  "gateway replicas without Redis|agentccGateway runs more than one replica without Redis|--set agentccGateway.replicas=2 --set agentccGateway.redis.enabled=false"
  "gateway Redis over TLS|the gateway has no Redis TLS|--set redis.mode=external --set redis.external.host=r --set redis.external.tls=true --set agentccGateway.redis.enabled=true"
  "a ClickHouse user the bundled server lacks|clickhouse.user must be default with clickhouse.mode=bundled|--set clickhouse.user=fa"
  "an autoscaled gateway with Redis over TLS|to a Redis without TLS, or run one replica with agentccGateway.redis.enabled=false|--set redis.mode=external --set redis.external.host=r --set redis.external.tls=true --set agentccGateway.autoscaling.enabled=true"
  "Enterprise without a license|edition=ee needs a license|--set edition=ee"
  "an unknown edition|re:edition.*must be one of|--set edition=enterprise"
  "two license sources|set license.existingSecret or license.key, not both|--set license.key=k --set license.existingSecret=s"
  "noProxy without a proxy|global.proxy.noProxy is set without|--set global.proxy.noProxy=.corp"
  "a proxy that is not a URL|re:httpsProxy.*oes not match pattern|--set global.proxy.httpsProxy=proxy:3128"
  "two CA bundle sources|global.caBundle.configMap or global.caBundle.secret, not both|--set global.caBundle.configMap=a --set global.caBundle.secret=b"
  "air-gapped serving without its volume|global.airgap with serving.enabled needs serving.persistence.enabled|--set global.airgap=true --set serving.enabled=true"
  "half an OAuth client|auth.google needs both clientId and clientSecret|--set auth.google.clientId=x"
  "an ExternalSecret the chart would not read|externalSecrets.secrets.app needs secrets.existingSecret|--set externalSecrets.enabled=true --set externalSecrets.secretStoreRef.name=vault --set externalSecrets.secrets.app.dataFrom[0].extract.key=a"
  "an unknown OpenShift mode|re:adaptSecurityContext.*must be one of|--set global.compatibility.openshift.adaptSecurityContext=on"
  "an unknown ExternalSecret group|re:[Pp]roperty ?[Nn]ame.*vault|--set externalSecrets.secrets.vault.dataFrom[0].extract.key=a"
)
for case in "${refused[@]}"; do
  IFS='|' read -r name expected args <<<"$case"
  # shellcheck disable=SC2086 # args is a word list
  if "$helm" template futureagi "$chart" -f "$chart/examples/bundled.yaml" $args >"$out/refused.txt" 2>&1; then
    fail "rendered despite: $name"
  fi
  if [[ $expected == re:* ]]; then
    expected=${expected#re:}
    grep_mode=-qE
  else
    grep_mode=-qF
  fi
  grep "$grep_mode" -- "$expected" "$out/refused.txt" || {
    cat "$out/refused.txt" >&2
    fail "the error for \"$name\" does not mention: $expected"
  }
  echo "ok   $name"
done
rm -f "$out/refused.txt"

echo "== install-time settings: a change to a live StatefulSet's volume is refused"
# `lookup` finds nothing under helm template: a copy of the chart reads the
# live StatefulSet from live.yaml instead.
live_chart="$out/live-chart"
rm -rf "$live_chart"
cp -R "$chart" "$live_chart"
"$python" - "$live_chart/templates/validate.yaml" <<'PY' || fail "validate.yaml no longer looks up the StatefulSet: update this check"
import sys
path = sys.argv[1]
lookup = 'lookup "apps/v1" "StatefulSet" $.Release.Namespace $name'
text = open(path).read()
assert text.count(lookup) == 1
open(path, "w").write(text.replace(lookup, '(get ($.Files.Get "live.yaml" | fromYaml) $name | default dict)'))
PY
# name|storageClassName of the live claim (none: absent)|its size|helm
# arguments (over examples/bundled.yaml)|expected message (empty: renders)
live_cases=(
  "the same volume|none|20Gi||"
  "the same size in other units|none|20Gi|--set postgres.bundled.persistence.size=20480Mi|"
  "a larger volume|none|20Gi|--set postgres.bundled.persistence.size=30Gi|postgres.bundled.persistence.size is 30Gi, but StatefulSet futureagi-postgres was created with 20Gi"
  "the same StorageClass|fast|20Gi|--set global.storageClass=fast|"
  "a StorageClass after the cluster default|none|20Gi|--set global.storageClass=fast|gives storageClassName \"fast\", but StatefulSet futureagi-postgres was created with no storageClassName"
  "no StorageClass (-) after the cluster default|none|20Gi|--set postgres.bundled.persistence.storageClass=-|gives storageClassName \"\" (\"-\"), but StatefulSet futureagi-postgres was created with no storageClassName"
  "the cluster default after no StorageClass (-)|\"\"|20Gi||gives no storageClassName (the cluster default), but StatefulSet futureagi-postgres was created with storageClassName \"\" (\"-\")"
  "no StorageClass (-) kept|\"\"|20Gi|--set global.storageClass=-|"
)
for case in "${live_cases[@]}"; do
  IFS='|' read -r name class size args expected <<<"$case"
  class_line=""
  [ "$class" = none ] || class_line="          storageClassName: $class"$'\n'
  printf 'futureagi-postgres:\n  spec:\n    volumeClaimTemplates:\n      - metadata: {name: data}\n        spec:\n%s          resources: {requests: {storage: %s}}\n' \
    "$class_line" "$size" >"$live_chart/live.yaml"
  # shellcheck disable=SC2086 # args is a word list
  if "$helm" template futureagi "$live_chart" -f "$chart/examples/bundled.yaml" $args >"$out/live.txt" 2>&1; then
    [ -z "$expected" ] || fail "rendered despite: $name"
  elif [ -z "$expected" ] || ! grep -qF -- "$expected" "$out/live.txt"; then
    cat "$out/live.txt" >&2
    fail "the error for \"$name\" does not mention: ${expected:-(it should render)}"
  fi
  echo "ok   $name"
done
rm -rf "$out/live.txt" "$live_chart"

for set in "${value_sets[@]}"; do
  name=${set%%|*}
  args=()
  for file in ${set#*|}; do
    args+=(-f "$chart/$file")
  done
  echo "== $name"
  "$helm" lint "$chart" --strict "${args[@]}" >"$out/$name.lint.txt" 2>&1 || {
    cat "$out/$name.lint.txt" >&2
    fail "helm lint ($name)"
  }
  "$helm" template futureagi "$chart" --namespace futureagi "${args[@]}" >"$out/$name.yaml"
  # Rendered for each Kubernetes version: some fields depend on it (the
  # kubelet's preStop sleep action needs 1.30).
  mkdir -p "$out/kube"
  for version in $kube_versions; do
    "$helm" template futureagi "$chart" --namespace futureagi --kube-version "$version" "${args[@]}" >"$out/kube/$name-$version.yaml"
    "$kubeconform" -strict -summary -kubernetes-version "$version" \
      -schema-location default -schema-location "$crd_schemas" "$out/kube/$name-$version.yaml" ||
      fail "kubeconform ($name, Kubernetes $version)"
  done
done

echo "== GitOps: rendering again changes nothing"
# Argo CD renders on every sync, where `lookup` finds nothing: with the
# keys in secrets.existingSecret, nothing may be generated anew.
"$helm" template futureagi "$chart" --namespace futureagi \
  -f "$chart/examples/external.yaml" -f "$chart/ci/gitops.yaml" >"$out/gitops.again.txt"
diff -u "$out/gitops.yaml" "$out/gitops.again.txt" || fail "a second render of gitops differs"
rm -f "$out/gitops.again.txt"
echo "ok   identical"

echo "== openshift-auto (the security.openshift.io/v1 API present)"
"$helm" template futureagi "$chart" --namespace futureagi -f "$chart/examples/bundled.yaml" \
  --api-versions security.openshift.io/v1 >"$out/openshift-auto.yaml"
echo "ok   rendered"

echo "== the install notes print the proxy's host, never its login"
# helm template leaves NOTES.txt out; a client-side dry run renders it. It
# never talks to the caller's cluster (no kubeconfig): Helm 3 wants one for a
# dry run, so it is skipped there.
if KUBECONFIG="$out/no-kubeconfig" "$helm" install futureagi "$chart" --dry-run=client --namespace futureagi \
  -f "$chart/examples/bundled.yaml" --set global.proxy.httpsProxy=http://corp:notes-123@proxy.corp.example:3128 \
  --set objectStorage.bundled.service.downloadPort=9100 >"$out/notes.txt" 2>&1; then
  sed -n '/^NOTES:/,$p' "$out/notes.txt" >"$out/notes-only.txt"
  if grep -q 'notes-123' "$out/notes-only.txt" || ! grep -qF 'through the proxy proxy.corp.example:3128;' "$out/notes-only.txt"; then
    cat "$out/notes-only.txt" >&2
    fail "the install notes print the proxy URL with its login"
  fi
  # MINIO_URL is http://localhost:<downloadPort>: the port-forward matches it.
  grep -qF 'port-forward svc/futureagi-minio 9100:9000' "$out/notes-only.txt" || {
    cat "$out/notes-only.txt" >&2
    fail "the install notes' MinIO port-forward ignores objectStorage.bundled.service.downloadPort"
  }
  # A login urlParse cannot parse ('#', a stray '%') still renders.
  KUBECONFIG="$out/no-kubeconfig" "$helm" install futureagi "$chart" --dry-run=client --namespace futureagi \
    -f "$chart/examples/bundled.yaml" --set-string 'global.proxy.httpsProxy=http://corp:n#o%zz@proxy.corp.example:3128' \
    >"$out/notes-odd.txt" 2>&1 || { cat "$out/notes-odd.txt" >&2; fail "install notes with an odd proxy login"; }
  grep -qF 'through the proxy proxy.corp.example:3128;' "$out/notes-odd.txt" || {
    sed -n '/^NOTES:/,$p' "$out/notes-odd.txt" >&2
    fail "the install notes misprint a proxy with an odd login"
  }
  echo "ok   host:port only"
elif grep -q 'cluster unreachable' "$out/notes.txt"; then
  echo "skip (this Helm needs a cluster for a client-side dry run)"
else
  cat "$out/notes.txt" >&2
  fail "helm install --dry-run=client"
fi

echo "== GitOps examples (not Helm values): YAML and CRD schemas"
for file in "$chart"/examples/gitops/*.yaml; do
  "$python" -c 'import sys, yaml; docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]; assert docs and all("kind" in d for d in docs), sys.argv[1]' "$file" ||
    fail "invalid YAML: $file"
  "$kubeconform" -strict -summary -schema-location default -schema-location "$crd_schemas" "$file" ||
    fail "kubeconform ($file)"
done

echo "== hack/support-bundle.sh parses and redacts"
bash -n "$chart/hack/support-bundle.sh" || fail "support-bundle.sh does not parse"
if command -v shellcheck >/dev/null; then
  shellcheck "$chart/hack/support-bundle.sh" || fail "shellcheck support-bundle.sh"
fi
# Its redaction functions, on values, log lines, describe output and the
# install notes with secrets in them: by name, and credentials in URLs (a
# proxy login, a DATABASE_URL in extraEnv) under any name.
redactors=$(sed -n '/^sensitive=/,/^run() {/p' "$chart/hack/support-bundle.sh" | sed '$d')
redacted=$(bash -c "$redactors"'
printf "license:\n  key: lic-123\n  url: https://licenses\npostgres:\n  password: pg-123\nsecrets:\n  extra:\n    SENTRY_DSN: dsn-123\n" | redact_yaml
printf "global:\n  proxy:\n    httpsProxy: http://corp:proxy-123@proxy.corp:3128\n    noProxy: .corp\nconfig:\n  extraEnv:\n    DATABASE_URL: postgres://app:url-123@db.corp:5432/app\n" | redact_yaml
printf "PG_PASSWORD:  env-123\napi_key=\"log-123\" user=bob\n" | redact_text
printf "      HTTPS_PROXY:   http://corp:describe-123@proxy.corp:3128\n      NO_PROXY:      .corp\n      DATABASE_URL:  postgres://app:dburl-123@db.corp:5432/app\n" | redact_text
printf "NOTE     Outbound traffic goes through http://corp:status-123@proxy.corp:3128; NO_PROXY covers\n" | redact_text
printf "config:\n  extraEnv:\n    DB_URL: postgres://app:p@ss-123@db.corp:5432/app\n" | redact_yaml')
if grep -qE 'lic-123|pg-123|dsn-123|env-123|log-123|proxy-123|url-123|describe-123|status-123|ss-123' <<<"$redacted"; then
  echo "$redacted" >&2
  fail "support-bundle.sh redaction leaves a secret"
fi
for kept in 'https://licenses' 'noProxy: .corp' 'NO_PROXY:      .corp' 'postgres://<redacted>@db.corp:5432/app' 'http://<redacted>@proxy.corp:3128;'; do
  grep -qF -- "$kept" <<<"$redacted" || {
    echo "$redacted" >&2
    fail "support-bundle.sh redaction removes: $kept"
  }
done
echo "ok   redacts"

echo "== rendered invariants"
# No manifest may carry an unexpanded template or an empty image.
if grep -nE '<no value>|image:( *| *"")$' "$out"/*.yaml; then
  fail "a rendered manifest has an unset value"
fi
# Inside the repository, the Python services also default their behaviour
# settings as docker-compose.distributed.yml does.
compose=()
if [ -f "$repo/docker-compose.distributed.yml" ]; then
  compose=(--compose "$repo/docker-compose.distributed.yml")
fi
"$python" "$chart/hack/rendered_checks.py" "$out" ${compose[@]+"${compose[@]}"}
# ... and they notice pods that read AGENTCC_WEBHOOK_SECRET from
# secrets.existingSecret while the chart's Secret holds the one given inline.
mkdir -p "$out/broken"
"$python" - "$out/all-components.yaml" >"$out/broken/all-components.yaml" <<'EOF'
import sys

import yaml


def point(node):
    if isinstance(node, dict):
        ref = node.get("valueFrom", {}).get("secretKeyRef")
        if node.get("name") == "AGENTCC_WEBHOOK_SECRET" and ref:
            ref["name"] = "futureagi-app"
        for value in node.values():
            point(value)
    elif isinstance(node, list):
        for value in node:
            point(value)


docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
point(docs)
yaml.safe_dump_all(docs, sys.stdout)
EOF
if "$python" "$chart/hack/rendered_checks.py" "$out/broken" >"$out/broken.txt" 2>&1; then
  fail "rendered_checks.py passes pods that read the webhook secret from the wrong Secret"
fi
grep -qF "no Secret holds AGENTCC_WEBHOOK_SECRET ('futureagi-app'" "$out/broken.txt" || {
  cat "$out/broken.txt" >&2
  fail "rendered_checks.py does not name the missing webhook secret"
}
rm -rf "$out/broken" "$out/broken.txt"
echo "ok   a webhook secret no Secret holds is caught"

echo "== values, schema and README"
"$python" "$chart/hack/values_docs.py" --check

echo "== the pre-commit formatter leaves the templates and generated files alone"
# scripts/lint-staged-root-format.mjs runs Prettier on staged YAML, JSON and
# Markdown: it cannot parse Go templates, and it would reformat what
# values_docs.py writes, which the check above compares byte for byte.
if [ -f "$repo/scripts/lint-staged-root-format.mjs" ]; then
  for path in "deploy/helm/*/templates/" deploy/helm/futureagi/values.schema.json deploy/helm/futureagi/README.md; do
    grep -qxF -- "$path" "$repo/.prettierignore" 2>/dev/null || fail ".prettierignore does not list $path"
  done
  echo "ok   ignored"
else
  echo "skip (not in the repository)"
fi

echo "== ClickHouse config files match the Standalone install"
if [ -d "$repo/deploy/standalone/clickhouse" ]; then
  diff -u "$repo/deploy/standalone/clickhouse/config.d/zz-small-host.xml" "$chart/files/clickhouse/config.d/zz-small-host.xml"
  diff -u "$repo/deploy/standalone/clickhouse/users.d/zz-small-host.xml" "$chart/files/clickhouse/users.d/zz-small-host.xml"
  diff -u "$repo/futureagi/.ci/clickhouse-storage-policy.xml" "$chart/files/clickhouse/config.d/storage-policy.xml"
  echo "ok   identical"
else
  echo "skip (not in the repository)"
fi

echo "== the UI's security headers match the frontend image's"
if [ -f "$repo/frontend/security-headers.conf" ]; then
  diff -u "$repo/frontend/security-headers.conf" "$chart/files/frontend/security-headers.conf"
  echo "ok   identical"
else
  echo "skip (not in the repository)"
fi

echo "all checks passed (manifests in $out)"

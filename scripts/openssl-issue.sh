#!/usr/bin/env bash
# Internal worker. Call through CertificateStore to hold the CA lock and record ownership.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
kind="$1"; shift
names=("$@")
name="${names[0]:-}"
: "${name:?certificate name is required}"
: "${rootpass:?CA password is required}"
: "${CERT_OUTPUT_BASE:?CertificateStore must supply an isolated output directory}"
base="$CERT_OUTPUT_BASE"
stamp="$(date -u +%Y%m%d-%H%M%S)-${RANDOM}"
target="$base/$stamp"
mkdir -p "$target"
if [[ "$kind" == server ]]; then
  cert_days="${days:-398}"
  san="$(python3 - "${names[@]}" <<'PY'
import ipaddress, sys
values = []
for name in sys.argv[1:]:
    try:
        values.append('IP:' + str(ipaddress.ip_address(name)))
    except ValueError:
        values.extend(['DNS:' + name] if name.startswith('*.') else ['DNS:*.' + name, 'DNS:' + name])
print(','.join(dict.fromkeys(values)))
PY
)"
else
  cert_days="${days:-365}"
fi
for algorithm in rsa ec; do
  file="$target/${algorithm}_$name"
  if [[ "$algorithm" == rsa ]]; then
    openssl genrsa -traditional -out "$file.key" 2048
  else
    openssl ecparam -genkey -name prime256v1 -out "$file.key"
  fi
  if [[ "$kind" == server ]]; then
    openssl req -new -key "$file.key" -out "$file.csr" \
      -subj "/C=CN/O=Corn Technology./CN=$name" -reqexts SAN \
      -config <(cat ca.cnf; printf '\n[SAN]\nsubjectAltName=%s\n' "$san")
    extension=server_cert
  else
    openssl req -new -key "$file.key" -out "$file.csr" \
      -subj "/C=CN/O=Corn Technology./CN=$name" -utf8
    extension=usr_cert
  fi
  openssl ca -config ca.cnf -batch -notext -extensions "$extension" \
    -in "$file.csr" -out "$file.crt" -cert "out/${algorithm}_root.crt" \
    -keyfile "out/${algorithm}_root.key" -passin env:rootpass -days "$cert_days"
  openssl pkcs12 -export -in "$file.crt" -inkey "$file.key" -out "$file.p12" -passout pass:1234
  openssl pkcs12 -export -legacy -in "$file.crt" -inkey "$file.key" -out "${file}_legacy.p12" -passout pass:1234
  openssl pkcs12 -in "$file.p12" -out "$file.pem" -passin pass:1234 -passout pass:1234
  cat "$file.crt" "out/${algorithm}_root.crt" > "$file.bundle.crt"
  cp "out/${algorithm}_root.crt" "$target/${algorithm}_root.crt"
done
printf '%s\n' "$target"

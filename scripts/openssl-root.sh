#!/usr/bin/env bash
# CertificateStore initializes state and holds the shared CA lock before calling this worker.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
: "${rootpass:?CA password is required}"
for algorithm in rsa ec; do
  if [[ -e "out/${algorithm}_root.crt" || -e "out/${algorithm}_root.key" ]]; then
    echo 'Existing CA material must be restored, never overwritten.' >&2
    exit 1
  fi
done
stage="$(mktemp -d out/.root-initialize.XXXXXXXX)"
trap 'rm -r -- "$stage"' EXIT
openssl ecparam -genkey -name prime256v1 | openssl ec -aes256 -out "$stage/ec_root.key" -passout env:rootpass
openssl req -new -x509 -config ca.cnf -key "$stage/ec_root.key" -out "$stage/ec_root.crt" \
  -subj '/C=CN/O=Corn Technology./CN=Corn Root CA' -passin env:rootpass -days 7300
openssl req -new -x509 -config ca.cnf -newkey rsa:2048 -keyout "$stage/rsa_root.key" \
  -out "$stage/rsa_root.crt" -subj '/C=CN/O=Corn Technology./CN=Corn Root CA' \
  -passout env:rootpass -days 7300
for file in "$stage"/*; do mv "$file" out/; done

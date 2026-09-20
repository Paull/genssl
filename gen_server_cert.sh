#!/usr/bin/env bash
set -e

if [ -z "$1" ]; then
  echo
  echo 'Issue a wildcard SSL certificate with Corn Root CA'
  echo
  echo "Usage: $0 <domain> [<domain2>] [<domain3>] [<domain4>] ..."
  echo '    <domain>          The domain name of your site, like "example.dev",'
  echo '                      you will get a certificate for *.example.dev'
  echo '                      Multiple domains are acceptable'
  exit
fi

if [ -z "${rootpass:-}" ]; then
  echo Root pass is not provided.
  exit 1
fi

SAN=""
for var in "$@"; do
  if test $(echo ${var} | grep -c '[a-zA-Z]') -gt 0; then
    SAN+="DNS:*.${var},DNS:${var},"
    echo ${SAN}
  else
    SAN+="IP:${var},"
    echo ${SAN}
  fi

done
SAN=${SAN:0:${#SAN}-1}

# Move to root directory
cd "$(dirname "${BASH_SOURCE[0]}")"

# Create domain directory
BASE_DIR="out/$1"
TIME=$(date +%Y%m%d-%H%M)
DIR="${BASE_DIR}/${TIME}"
mkdir -p ${DIR}

# 证书天数
if [ -z "${days:-}" ]; then days=398; fi

# ------------------------------- RSA ---------------------------------
# check if root certificate exists
if [ ! -f "out/rsa_root.crt" ]; then
  echo "rsa root证书不存在"
  exit 1
fi

# Generate key
if [ -f "${BASE_DIR}/rsa_$1.key" ]; then
  echo "Reusing existing RSA private key"
  cp "${BASE_DIR}/rsa_$1.key" "${DIR}/rsa_$1.key"
else
  openssl genrsa -traditional -out "${DIR}/rsa_$1.key" 2048
fi

# Create CSR
openssl req -new -out "${DIR}/rsa_$1.csr" \
  -key ${DIR}/rsa_$1.key \
  -reqexts SAN \
  -config <(cat ca.cnf \
    <(printf "[SAN]\nsubjectAltName=${SAN}")) \
  -subj "/C=CN/O=Corn Technology./CN=$1"

# Issue certificate
openssl ca -config ./ca.cnf -batch -notext \
  -in "${DIR}/rsa_$1.csr" \
  -out "${DIR}/rsa_$1.crt" \
  -cert ./out/rsa_root.crt \
  -keyfile ./out/rsa_root.key -passin env:rootpass -days ${days}

# 保存serial文件
mv ./out/serial.old ${DIR}/serial
echo "CA serial: $(cat ${DIR}/serial)"

# 生客户端p12格式证书，需要输入一个密码，选一个好记的，比如123456
openssl pkcs12 -export \
  -in "${DIR}/rsa_$1.crt" \
  -inkey "${DIR}/rsa_$1.key" \
  -out "${DIR}/rsa_$1.p12" \
  -passout pass:1234

# 生客户端Legacy p12格式证书，需要输入一个密码，选一个好记的，比如123456
openssl pkcs12 \
  -in "${DIR}/rsa_$1.crt" \
  -inkey "${DIR}/rsa_$1.key" \
  -export -legacy \
  -out "${DIR}/rsa_$1_legacy.p12" \
  -passout pass:1234

# 生成客户端pem格式证书
openssl pkcs12 \
  -in "${DIR}/rsa_$1.p12" \
  -out "${DIR}/rsa_$1.pem" \
  -passin pass:1234 \
  -passout pass:1234

# Chain certificate with CA
cat "${DIR}/rsa_$1.crt" ./out/rsa_root.crt >"${DIR}/rsa_$1.bundle.crt"

# Create Soft Links
ln -snf "./${TIME}/rsa_$1.bundle.crt" "${BASE_DIR}/rsa_$1.bundle.crt"
ln -snf "./${TIME}/rsa_$1.crt" "${BASE_DIR}/rsa_$1.crt"
ln -snf "./${TIME}/rsa_$1.key" "${BASE_DIR}/rsa_$1.key"
ln -snf "./${TIME}/rsa_$1.p12" "${BASE_DIR}/rsa_$1.p12"
ln -snf "./${TIME}/rsa_$1_legacy.p12" "${BASE_DIR}/rsa_$1_legacy.p12"
ln -snf "./${TIME}/rsa_$1.pem" "${BASE_DIR}/rsa_$1.pem"
ln -snf "../rsa_root.crt" "${BASE_DIR}/rsa_root.crt"

# ------------------------------- EC ---------------------------------
# check if root certificate exists
if [ ! -f "out/ec_root.crt" ]; then
  echo "ec root证书不存在"
  exit 1
fi

# Generate key
if [ -f "${BASE_DIR}/ec_$1.key" ]; then
  echo "Reusing existing EC private key"
  cp "${BASE_DIR}/ec_$1.key" "${DIR}/ec_$1.key"
else
  openssl ecparam -genkey -name prime256v1 -out "${DIR}/ec_$1.key"
fi

# Create CSR
openssl req -new -out "${DIR}/ec_$1.csr" \
  -key ${DIR}/ec_$1.key \
  -reqexts SAN \
  -config <(cat ca.cnf \
    <(printf "[SAN]\nsubjectAltName=${SAN}")) \
  -subj "/C=CN/O=Corn Technology./CN=$1" \
  -sha256

# Issue certificate
openssl ca -config ./ca.cnf -batch -notext \
  -in "${DIR}/ec_$1.csr" \
  -out "${DIR}/ec_$1.crt" \
  -cert ./out/ec_root.crt \
  -keyfile ./out/ec_root.key -passin env:rootpass -days ${days}

# 保存serial文件
mv ./out/serial.old ${DIR}/serial
echo "CA serial: $(cat ${DIR}/serial)"

# 生客户端p12格式证书，需要输入一个密码，选一个好记的，比如123456
openssl pkcs12 -export \
  -in "${DIR}/ec_$1.crt" \
  -inkey "${DIR}/ec_$1.key" \
  -out "${DIR}/ec_$1.p12" \
  -passout pass:1234

# 生客户端Legacy p12格式证书，需要输入一个密码，选一个好记的，比如123456
openssl pkcs12 \
  -in "${DIR}/ec_$1.crt" \
  -inkey "${DIR}/ec_$1.key" \
  -export -legacy \
  -out "${DIR}/ec_$1_legacy.p12" \
  -passout pass:1234

# 生成客户端pem格式证书
openssl pkcs12 \
  -in "${DIR}/ec_$1.p12" \
  -out "${DIR}/ec_$1.pem" \
  -passin pass:1234 \
  -passout pass:1234

# Chain certificate with CA
cat "${DIR}/ec_$1.crt" ./out/ec_root.crt >"${DIR}/ec_$1.bundle.crt"

# Create Soft Links
ln -snf "./${TIME}/ec_$1.bundle.crt" "${BASE_DIR}/ec_$1.bundle.crt"
ln -snf "./${TIME}/ec_$1.crt" "${BASE_DIR}/ec_$1.crt"
ln -snf "./${TIME}/ec_$1.key" "${BASE_DIR}/ec_$1.key"
ln -snf "./${TIME}/ec_$1.p12" "${BASE_DIR}/ec_$1.p12"
ln -snf "./${TIME}/ec_$1_legacy.p12" "${BASE_DIR}/ec_$1_legacy.p12"
ln -snf "./${TIME}/ec_$1.pem" "${BASE_DIR}/ec_$1.pem"
ln -snf "../ec_root.crt" "${BASE_DIR}/ec_root.crt"

# ------------------------------- output ---------------------------------

# Output certificates
echo
echo "Certificates are located in:"

LS=$([[ $(ls --help | grep '\-\-color') ]] && echo "ls --color" || echo "ls -G")

${LS} -la $(pwd)/${BASE_DIR}/*.*

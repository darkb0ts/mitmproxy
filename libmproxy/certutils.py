import subprocess, os, ssl, hashlib, socket
from pyasn1.type import univ, constraint, char, namedtype, tag
from pyasn1.codec.der.decoder import decode
import OpenSSL
import utils

CERT_SLEEP_TIME = 1
CERT_EXPIRY = str(365 * 3)


def dummy_ca(path):
    dirname = os.path.dirname(path)
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    if path.endswith(".pem"):
        basename, _ = os.path.splitext(path)
    else:
        basename = path

    key = OpenSSL.crypto.PKey()
    key.generate_key(OpenSSL.crypto.TYPE_RSA, 1024)
    ca = OpenSSL.crypto.X509()
    ca.set_version(3)
    ca.set_serial_number(1)
    ca.get_subject().CN = "mitmproxy"
    ca.get_subject().OU = "mitmproxy"
    ca.gmtime_adj_notBefore(0)
    ca.gmtime_adj_notAfter(24 * 60 * 60 * 720)
    ca.set_issuer(ca.get_subject())
    ca.set_pubkey(key)
    ca.add_extensions([
      OpenSSL.crypto.X509Extension("basicConstraints", True,
                                   "CA:TRUE"),
      OpenSSL.crypto.X509Extension("nsCertType", True,
                                   "sslCA"),
      OpenSSL.crypto.X509Extension("extendedKeyUsage", True,
                                    "serverAuth,clientAuth,emailProtection,timeStamping,msCodeInd,msCodeCom,msCTLSign,msSGC,msEFS,nsSGC"
                                    ),
      OpenSSL.crypto.X509Extension("keyUsage", True,
                                   "keyCertSign, cRLSign"),
      OpenSSL.crypto.X509Extension("subjectKeyIdentifier", False, "hash",
                                   subject=ca),
      ])
    ca.sign(key, "sha1")

    # Dump the CA plus private key
    f = open(path, "w")
    f.write(OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, key))
    f.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, ca))
    f.close()

    # Dump the certificate in PEM format
    f = open(os.path.join(dirname, basename + "-cert.pem"), "w")
    f.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, ca))
    f.close()

    # Dump the certificate in PKCS12 format for Windows devices
    f = open(os.path.join(dirname, basename + "-cert.p12"), "w")
    p12 = OpenSSL.crypto.PKCS12()
    p12.set_certificate(ca)
    f.write(p12.export())
    f.close()
    return True


def dummy_cert(certdir, ca, commonname, sans):
    """
        certdir: Certificate directory.
        ca: Path to the certificate authority file, or None.
        commonname: Common name for the generated certificate.

        Returns cert path if operation succeeded, None if not.
    """
    namehash = hashlib.sha256(commonname).hexdigest()
    certpath = os.path.join(certdir, namehash + ".pem")
    if os.path.exists(certpath):
        return certpath

    confpath = os.path.join(certdir, namehash + ".cnf")
    reqpath = os.path.join(certdir, namehash + ".req")

    template = open(utils.pkg_data.path("resources/cert.cnf")).read()

    ss = []
    for i, v in enumerate(sans):
        ss.append("DNS.%s = %s"%(i+1, v))
    ss = "\n".join(ss)

    f = open(confpath, "w")
    f.write(
        template%(
            dict(
                commonname=commonname,
                sans=ss,
                altnames="subjectAltName = @alt_names" if ss else ""
            )
        )
    )
    f.close()

    if ca:
        # Create a dummy signed certificate. Uses same key as the signing CA
        cmd = [
            "openssl",
            "req",
            "-new",
            "-config", confpath,
            "-out", reqpath,
            "-key", ca,
        ]
        ret = subprocess.call(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE
        )
        if ret: return None
        cmd = [
            "openssl",
            "x509",
            "-req",
            "-in", reqpath,
            "-days", CERT_EXPIRY,
            "-out", certpath,
            "-CA", ca,
            "-CAcreateserial",
            "-extfile", confpath,
            "-extensions", "v3_cert_req",
        ]
        ret = subprocess.call(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE
        )
        if ret: return None
    else:
        # Create a new selfsigned certificate + key
        cmd = [
            "openssl",
            "req",
            "-new",
            "-x509",
            "-config", confpath,
            "-nodes",
            "-days", CERT_EXPIRY,
            "-out", certpath,
            "-newkey", "rsa:1024",
            "-keyout", certpath,
        ]
        ret = subprocess.call(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE
        )
        if ret: return None
    return certpath


class _GeneralName(univ.Choice):
    # We are only interested in dNSNames. We use a default handler to ignore
    # other types.
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('dNSName', char.IA5String().subtype(
                implicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatSimple, 2)
            )
        ),
    )


class _GeneralNames(univ.SequenceOf):
    componentType = _GeneralName()
    sizeSpec = univ.SequenceOf.sizeSpec + constraint.ValueSizeConstraint(1, 1024)



class SSLCert:
    def __init__(self, pemtxt):
        """
            Returns a (common name, [subject alternative names]) tuple.
        """
        self.cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, pemtxt)

    @property
    def cn(self):
        cn = None
        for i in self.cert.get_subject().get_components():
            if i[0] == "CN":
                cn = i[1]
        return cn

    @property
    def altnames(self):
        altnames = []
        for i in range(self.cert.get_extension_count()):
            ext = self.cert.get_extension(i)
            if ext.get_short_name() == "subjectAltName":
                dec = decode(ext.get_data(), asn1Spec=_GeneralNames())
                for i in dec[0]:
                    altnames.append(i[0])
        return altnames


def get_remote_cert(host, port):
    addr = socket.gethostbyname(host)
    s = ssl.get_server_certificate((addr, port))
    return SSLCert(s)



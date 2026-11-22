import os
import tempfile

# 在导入 app 之前指定临时数据库
_TMP = tempfile.mkdtemp(prefix="proof-test-")
os.environ["PROOF_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ.setdefault("PROOF_KEEP_TMP", "1")

from pathlib import Path


CODEGEN_MARKER_PYTHON = Path(
    "D:/Anaconda/envs/codegen-marker/python.exe"
)


def codegen_python_executable() -> str:
    if not CODEGEN_MARKER_PYTHON.is_file():
        raise RuntimeError(
            "Required codegen-marker interpreter is missing: "
            f"{CODEGEN_MARKER_PYTHON}"
        )
    return str(CODEGEN_MARKER_PYTHON)

"""Install in an isolated virtual environment, then build the React UI."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]

def main():
    if sys.version_info < (3, 11):
        raise SystemExit('请安装 Python 3.11 或更高版本。')
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise SystemExit('请先安装 Node.js 22.12+（或 20.19+），重新打开终端后再试。')
    env = ROOT / '.venv'
    if not env.exists():
        venv.create(env, with_pip=True)
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
    subprocess.run([npm, 'ci'], cwd=ROOT / 'frontend', check=True)
    subprocess.run([npm, 'run', 'build'], cwd=ROOT / 'frontend', check=True)
    print('\n安装完成。Windows 双击 start.cmd；其他系统运行 .venv/bin/python run.py。')

if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f'安装中断（退出码 {exc.returncode}）。解决上方错误后重新运行 setup.cmd。')

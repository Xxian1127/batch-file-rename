# -*- coding: utf-8 -*-
"""
批量重命名文件脚本 batch_rename.py
==================================
功能：
  1. 序号模式（sequence）：按序号批量重命名，支持自定义前缀、起始编号、
     保留原文件名，例如 "img001.jpg"、"照片_001_旅行.jpg"。
  2. 替换模式（replace）：将文件名中的指定字符串批量替换为另一字符串。

安全性设计：
  - 扩展名自动保留，绝不参与重命名逻辑。
  - 目标目录扫描使用 sorted() 稳定排序，结果可复现。
  - 重名冲突自动处理：默认跳过，可选追加编号（-1、-2 ...）避免覆盖。
  - 采用"两阶段改名"（先改名到临时名，再改到最终名），彻底避免
    中间态互相覆盖，防止文件丢失。
  - 执行前打印完整预览清单并请求用户输入 y 确认。

使用方法：
  python batch_rename.py --dir "D:/图片" --mode sequence --prefix img --start 1
  python batch_rename.py --dir "D:/文档" --mode replace --find 旧词 --replace 新词
  不带任何参数运行则进入交互式引导模式。

作者：Marvis File Agent
"""

import argparse
import os
import sys
import uuid

# ---------------------------------------------------------------------------
# 一、核心重命名逻辑
# ---------------------------------------------------------------------------

def collect_files(folder):
    """
    扫描目标目录下的所有普通文件（不含子目录、隐藏文件），按名称稳定排序。
    返回排序后的文件名列表。
    """
    if not os.path.isdir(folder):
        print(f"[错误] 目录不存在: {folder}")
        sys.exit(1)

    names = []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        # 只处理文件，跳过目录
        if os.path.isfile(full):
            # 跳过隐藏文件（以 . 开头，例如 .DS_Store）
            if name.startswith("."):
                continue
            names.append(name)
    return names


def build_new_name_sequence(original, prefix, start_no, digits, keep_name):
    """
    按序号模式生成目标文件名（不含扩展名部分由调用方补回）。
    original: 原文件名（含扩展名）
    prefix  : 自定义前缀，如 "img"
    start_no: 起始编号
    digits  : 编号位数，如 3 表示 001
    keep_name: 是否保留原文件名（去除扩展名后）
    返回新文件名（含扩展名）。
    """
    stem, ext = os.path.splitext(original)          # 拆分主名与扩展名
    number = str(start_no).zfill(digits)            # 补零，如 001
    if keep_name:
        new_stem = f"{prefix}{number}_{stem}"
    else:
        new_stem = f"{prefix}{number}"
    return new_stem + ext                            # 扩展名原样保留


def build_new_name_replace(original, find_str, replace_str):
    """
    按替换模式生成目标文件名。
    只替换主名部分，扩展名中的字符串一概不替换。
    """
    stem, ext = os.path.splitext(original)
    new_stem = stem.replace(find_str, replace_str)
    return new_stem + ext


def resolve_conflict(folder, desired, used_targets, strategy):
    """
    处理重名冲突。
    desired      : 想要的目标文件名
    used_targets : 已被本次重命名占用的目标名集合
    strategy     : "skip" 跳过；"auto" 追加编号（-1、-2 ...）
    返回 (最终文件名, 是否冲突被处理过)；若 skip 且冲突，返回 (None, False)。
    """
    from os.path import exists, join

    # 冲突 = 与磁盘上已有文件重名（且不是自己）或与本次其它目标重名
    def conflicted(name):
        return name in used_targets or exists(join(folder, name))

    if not conflicted(desired):
        return desired, False

    if strategy == "skip":
        return None, False

    # auto：在扩展名前追加 -1、-2 ...
    stem, ext = os.path.splitext(desired)
    n = 1
    while True:
        candidate = f"{stem}-{n}{ext}"
        if not conflicted(candidate):
            return candidate, True
        n += 1


def plan_renames(folder, names, mode, opts):
    """
    生成重命名计划。返回列表，每个元素为
    (原文件名, 最终文件名, 处理结果说明)。
    此处不真正执行任何改名操作。
    """
    used_targets = set()          # 本次计划中已占用的目标名
    plan = []
    counter = opts["start"]       # 序号递增计数器（仅序号模式使用）

    for name in names:
        if mode == "sequence":
            new_name = build_new_name_sequence(
                name, opts["prefix"], counter, opts["digits"], opts["keep_name"]
            )
            counter += 1          # 每个文件分配独立递增编号，避免重号
        else:  # replace
            if opts["find"] not in name:
                # 不含查找串的文件保持原名不动
                plan.append((name, name, "未命中，保持原名"))
                used_targets.add(name)
                continue
            new_name = build_new_name_replace(name, opts["find"], opts["replace"])

        if new_name == name:
            # 原名和目标名完全一致，无需改名
            plan.append((name, name, "名称不变，跳过"))
            used_targets.add(name)
            continue

        final_name, conflicted = resolve_conflict(
            folder, new_name, used_targets, opts["conflict"]
        )
        if final_name is None:
            plan.append((name, name, "目标重名，已按策略跳过"))
            used_targets.add(name)
            continue

        note = "冲突后追加编号" if conflicted else ""
        plan.append((name, final_name, note))
        used_targets.add(final_name)

    return plan


def execute_renames(folder, plan):
    """
    执行重命名计划，采用两阶段改名：
      第一阶段：把所有要改名的文件统一改成唯一的临时名。
      第二阶段：再统一从临时名改成最终名。
    这样任意时刻都不会出现"目标文件已存在被覆盖"的情况。
    返回 (成功数, 失败列表)。
    """
    staged = []                       # 记录 (临时名, 最终名)
    failures = []

    # 第一阶段：改成临时名
    for old, new, _ in plan:
        if old == new:
            continue
        tmp_name = f".__rename_{uuid.uuid4().hex}.tmp"
        tmp_path = os.path.join(folder, tmp_name)
        old_path = os.path.join(folder, old)
        try:
            os.rename(old_path, tmp_path)
            staged.append((tmp_path, os.path.join(folder, new)))
        except OSError as e:
            failures.append((old, new, f"第一阶段失败: {e}"))

    # 第二阶段：改成最终名
    for tmp_path, final_path in staged:
        try:
            os.rename(tmp_path, final_path)
        except OSError as e:
            # 恢复失败时尽力把临时名改回原状态说明，但至少文件未丢失
            failures.append((os.path.basename(tmp_path),
                             os.path.basename(final_path),
                             f"第二阶段失败: {e}"))

    succeed = len(staged) - len([f for f in failures])
    return succeed, failures


# ---------------------------------------------------------------------------
# 二、交互式引导
# ---------------------------------------------------------------------------

def interactive_mode():
    """无命令行参数时进入交互模式，逐项询问用户。"""
    print("===== 批量重命名文件（交互模式） =====")
    folder = input("请输入目标文件夹路径: ").strip().strip('"').strip("'")
    while not os.path.isdir(folder):
        folder = input("[提醒] 目录无效，请重新输入: ").strip().strip('"').strip("'")

    mode = input("请选择模式（1=按序号, 2=替换字符串）: ").strip()
    opts = {"conflict": "auto"}   # 交互模式默认冲突追加编号，更友好

    if mode == "1":
        prefix = input("前缀（直接回车为空）: ").strip()
        start = input("起始编号（回车默认 1）: ").strip() or "1"
        digits = input("编号位数（回车默认 3，如 001）: ").strip() or "3"
        keep = input("是否保留原文件名？(y/n, 回车默认 n): ").strip().lower()
        opts.update({
            "prefix": prefix,
            "start": int(start),
            "digits": int(digits),
            "keep_name": keep == "y",
        })
    elif mode == "2":
        find_str = input("请输入要查找的字符串: ")
        replace_str = input("请输入替换为的字符串（回车表示删除该串）: ")
        opts.update({"find": find_str, "replace": replace_str})
    else:
        print("[错误] 未知模式，退出。")
        sys.exit(1)

    return folder, ("replace" if mode == "2" else "sequence"), opts


# ---------------------------------------------------------------------------
# 三、命令行解析
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="批量重命名文件：支持按序号与替换字符串两种模式"
    )
    parser.add_argument("--dir", help="目标文件夹路径")
    parser.add_argument(
        "--mode", choices=["sequence", "replace"],
        help='重命名模式：sequence=按序号，replace=替换字符串'
    )
    # 序号模式参数
    parser.add_argument("--prefix", default="", help="序号模式：前缀，如 img")
    parser.add_argument("--start", type=int, default=1, help="序号模式：起始编号，默认 1")
    parser.add_argument("--digits", type=int, default=3, help="序号模式：编号位数，默认 3")
    parser.add_argument("--keep-name", action="store_true",
                        help="序号模式：保留原文件名（追加在前缀编号后）")
    # 替换模式参数
    parser.add_argument("--find", default="", help="替换模式：要查找的字符串")
    parser.add_argument("--replace", default="", help="替换模式：替换为的字符串")
    # 通用参数
    parser.add_argument("--conflict", choices=["skip", "auto"], default="skip",
                        help="重名冲突处理：skip=跳过(默认)，auto=追加编号")
    parser.add_argument("--yes", action="store_true", help="跳过运行前确认（谨慎使用）")
    return parser


# ---------------------------------------------------------------------------
# 四、主流程
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    # 无 --dir 时进入交互模式
    if not args.dir:
        folder, mode, opts = interactive_mode()
    else:
        folder = args.dir.strip().strip('"').strip("'")
        mode = args.mode
        opts = {
            "prefix": args.prefix,
            "start": args.start,
            "digits": args.digits,
            "keep_name": args.keep_name,
            "find": args.find,
            "replace": args.replace,
            "conflict": args.conflict,
        }
        # 命令行参数合法性校验
        if mode == "replace" and not args.find:
            print("[错误] 替换模式必须提供 --find 参数。")
            sys.exit(1)
        if args.start < 0:
            print("[错误] --start 起始编号不能为负数。")
            sys.exit(1)
        if args.digits < 1:
            print("[错误] --digits 编号位数至少为 1。")
            sys.exit(1)

    if not os.path.isdir(folder):
        print(f"[错误] 目录不存在: {folder}")
        sys.exit(1)

    # 1. 扫描
    names = collect_files(folder)
    if not names:
        print("[提示] 目标目录中没有可处理的文件。")
        return

    # 2. 生成重命名计划
    plan = plan_renames(folder, names, mode, opts)

    # 3. 打印预览清单，请求确认
    print(f"\n目标目录: {folder}  （共 {len(names)} 个文件）")
    print("=" * 70)
    print(f"{'原文件名':<40} | {'新文件名':<40} | 说明")
    print("-" * 70)
    changed_count = 0
    for old, new, note in plan:
        marker = "==>" if old != new else "   "
        print(f"{old:<40} {marker} {new:<40} {note}")
        if old != new:
            changed_count += 1
    print("=" * 70)
    print(f"即将重命名 {changed_count} 个文件（其余名称不变）。")

    if changed_count == 0:
        print("[提示] 没有文件需要改名，退出。")
        return

    # 4. 用户确认
    if not args.yes:
        answer = input("\n是否继续执行？输入 y 确认执行，其它任意键取消: ").strip().lower()
        if answer != "y":
            print("已取消，未做任何修改。")
            return

    # 5. 执行
    succeed, failures = execute_renames(folder, plan)
    print(f"\n完成：成功重命名 {succeed} 个文件。")
    if failures:
        print("失败明细：")
        for old, new, err in failures:
            print(f"  - {old} -> {new}: {err}")
        print("[提示] 失败的文件仍保留在目录中，未造成丢失。")


if __name__ == "__main__":
    main()

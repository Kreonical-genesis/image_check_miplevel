#!/usr/bin/env python3
import os
import zipfile
import io
import math
import argparse
import json
from typing import Optional, Tuple
from PIL import Image

def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0

def mipmap_levels_needed(max_dim: int) -> int:
    if max_dim <= 0:
        return 0
    # возвращаем ceil(log2(max_dim)) — число шагов, чтобы дойти до 1
    return math.ceil(math.log2(max_dim))

def parse_mcmeta_for_frames(z: zipfile.ZipFile, png_name: str, img_w: int, img_h: int) -> Tuple[bool, Optional[int], Optional[int], str]:
    """
    Ищет png_name + ".mcmeta" в zip, парсит и пытается вернуть (found, frame_w, frame_h, info).
    Если frame_w/frame_h известны — возвращаем их (ints). Если найден mcmeta, но не получилось
    извлечь размер кадров — возвращаем (True, None, None, info).
    Если не найден — (False, None, None, "").
    """
    meta_name = png_name + ".mcmeta"
    try:
        meta_data = z.read(meta_name)
    except KeyError:
        return (False, None, None, "")

    info_text = ""
    try:
        jm = json.loads(meta_data.decode('utf-8'))
    except Exception as e:
        return (True, None, None, f"mcmeta parse error: {e}")

    anim = jm.get("animation") if isinstance(jm, dict) else None
    if not anim or not isinstance(anim, dict):
        return (True, None, None, "mcmeta found but no 'animation' object")

    # 1) если явно указаны width/height в анимации
    aw = anim.get("width")
    ah = anim.get("height")
    if isinstance(aw, int) and isinstance(ah, int) and aw > 0 and ah > 0:
        # проверим, что картинка делится на эти кадры
        cols = img_w // aw if aw > 0 else 0
        rows = img_h // ah if ah > 0 else 0
        if img_w % aw == 0 and img_h % ah == 0 and cols * rows >= 1:
            frames_possible = cols * rows
            info_text = f"mcmeta: frame {aw}x{ah}, possible frames {frames_possible}"
            return (True, aw, ah, info_text)
        else:
            info_text = f"mcmeta has frame {aw}x{ah} but image {img_w}x{img_h} not divisible by that"
            return (True, None, None, info_text)

    # 2) если заданы frames (список), но нет размера — попробуем инферировать
    frames = anim.get("frames")
    if isinstance(frames, list) and len(frames) > 0:
        # Популярный кейс: вертикальная лента квадратных кадров (ширина == frame_w)
        # Пробуем несколько эвристик:
        # a) если img_h % img_w == 0 -> вертикальные квадратные кадры img_w x img_w
        if img_w > 0 and img_h % img_w == 0:
            frame_w = img_w
            frame_h = img_w
            frames_possible = img_h // frame_h
            if len(frames) <= frames_possible:
                info_text = f"mcmeta: frames list len {len(frames)}, inferred frame {frame_w}x{frame_h}, possible {frames_possible}"
                return (True, frame_w, frame_h, info_text)
            else:
                info_text = f"mcmeta: frames list len {len(frames)}, inferred frame {frame_w}x{frame_h} but not enough space (possible {frames_possible})"
                return (True, None, None, info_text)

        # b) горизонтальная лента квадратных кадров (img_w % img_h == 0)
        if img_h > 0 and img_w % img_h == 0:
            frame_h = img_h
            frame_w = img_h
            frames_possible = img_w // frame_w
            if len(frames) <= frames_possible:
                info_text = f"mcmeta: frames list len {len(frames)}, inferred frame {frame_w}x{frame_h}, possible {frames_possible}"
                return (True, frame_w, frame_h, info_text)
            else:
                info_text = f"mcmeta: frames list len {len(frames)}, inferred frame {frame_w}x{frame_h} but not enough space (possible {frames_possible})"
                return (True, None, None, info_text)

        # c) если frames — список объектов или индексов, но картинка имеет целые множители какого-то tile-size
        #    попробуем найти наименьший целый делитель для высоты или ширины, который даёт >= len(frames)
        lf = len(frames)
        # пробуем по вертикали: найти h, такой что img_h % h == 0 и (img_h // h) >= lf и img_w == h or img_w % (img_w // (img_w // h)) == 0
        # упростим: если img_w % 1 ==0 всегда. Попробуем вариант: кадры имеют ширину img_w и высоту img_h / lf если целое
        if img_h % lf == 0:
            frame_h = img_h // lf
            frame_w = img_w
            if frame_h > 0:
                info_text = f"mcmeta: frames list len {lf}, inferred vertical frames {frame_w}x{frame_h}"
                return (True, frame_w, frame_h, info_text)
        if img_w % lf == 0:
            frame_w = img_w // lf
            frame_h = img_h
            info_text = f"mcmeta: frames list len {lf}, inferred horizontal frames {frame_w}x{frame_h}"
            return (True, frame_w, frame_h, info_text)

    # 3) Если нет полезной информации — возвращаем, что mcmeta есть, но бессодержателен для нас
    return (True, None, None, "mcmeta found but couldn't infer useful frame size")

def process_zip(zip_path: str, max_allowed_levels: int, tile_size: int = 0):
    npot = []
    high_mip = []
    mcmeta_ok = []      # pngs accepted because of mcmeta (with info)
    mcmeta_other = []   # mcmeta found but not helpful
    errors = []

    with zipfile.ZipFile(zip_path, 'r') as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if not name.lower().endswith('.png'):
                continue

            try:
                data = z.read(info)
                img = Image.open(io.BytesIO(data))
                width, height = img.size

                # пробуем прочитать mcmeta (если есть)
                mc_found, frame_w, frame_h, mc_info = parse_mcmeta_for_frames(z, name, width, height)

                # эффективный размер для расчёта mip — либо размер кадра (если есть), либо сам png
                if frame_w and frame_h:
                    eff_max_dim = max(frame_w, frame_h)
                else:
                    eff_max_dim = max(width, height)

                levels = mipmap_levels_needed(eff_max_dim)

                # NPOT логика: считается NPOT только если:
                #  - не является power-of-two (оба измерения)
                #  - и не кратно tile_size (если указано)
                #  - и нет mcmeta, которое объясняет и делает её допустимой
                is_powa = is_power_of_two(width) and is_power_of_two(height)
                is_tile_multiple = (tile_size > 0 and width % tile_size == 0 and height % tile_size == 0)

                if not is_powa and not is_tile_multiple:
                    if mc_found and frame_w and frame_h:
                        # mcmeta объясняет размеры — считаем как mcmeta_ok (не попадает в npot)
                        mcmeta_ok.append(f"{name} ({width}x{height}) — mcmeta frame {frame_w}x{frame_h} | {mc_info}")
                    elif mc_found:
                        # mcmeta есть, но не дал размеров — пометим отдельно (и также в npot, т.к. непонятно)
                        mcmeta_other.append(f"{name} ({width}x{height}) — mcmeta present but not conclusive: {mc_info}")
                        npot.append(f"{name} ({width}x{height}) — mcmeta present but not conclusive")
                    else:
                        npot.append(f"{name} ({width}x{height})")

                # High mip: проверяем уже по eff_max_dim (учитывая кадр из mcmeta если есть)
                if levels > max_allowed_levels:
                    reason = f"needs {levels} mip levels (based on {'frame' if (frame_w and frame_h) else 'image'} {eff_max_dim}px) — size {width}x{height}"
                    # если mcmeta есть, добавим краткую инфу
                    if mc_found:
                        reason += f" | mcmeta: {mc_info}"
                    high_mip.append(f"{name} — {reason}")

            except Exception as e:
                errors.append(f"{name} — error: {e}")

    # записи
    base = os.path.splitext(os.path.basename(zip_path))[0]
    out_npot = base + "_npot.txt"
    out_high = base + "_high_mip.txt"
    out_mcmeta = base + "_mcmeta.txt"
    out_errors = base + "_errors.txt"

    with open(out_npot, "w", encoding="utf-8") as f:
        if npot:
            f.write("\n".join(npot))
        else:
            f.write("Нет NPOT PNG (все размеры — степени двойки или допустимы по tile-size или mcmeta).")

    with open(out_high, "w", encoding="utf-8") as f:
        if high_mip:
            f.write(f"Текущая допустимая глубина mip levels = {max_allowed_levels}\n\n")
            f.write("\n".join(high_mip))
        else:
            f.write(f"Нет текстур, требующих больше {max_allowed_levels} mip уровней.")

    with open(out_mcmeta, "w", encoding="utf-8") as f:
        lines = []
        if mcmeta_ok:
            lines.append("=== Приняты по mcmeta (mcmeta указывает/инферирует корректный размер кадров) ===")
            lines.extend(mcmeta_ok)
            lines.append("")
        if mcmeta_other:
            lines.append("=== mcmeta присутствует, но не дало однозначной информации ===")
            lines.extend(mcmeta_other)
        if not lines:
            f.write("Нет png с mcmeta, подходящих под условия.")
        else:
            f.write("\n".join(lines))

    if errors:
        with open(out_errors, "w", encoding="utf-8") as f:
            f.write("\n".join(errors))

    return len(npot), len(high_mip), len(mcmeta_ok) + len(mcmeta_other), len(errors)

def main():
    parser = argparse.ArgumentParser(description="Проверяет PNG в zip на NPOT / mip-уровни с поддержкой .mcmeta анимаций.")
    parser.add_argument("--dir", "-d", default=None,
                        help="Папка для поиска zip (по умолчанию — папка скрипта).")
    parser.add_argument("--max-mip", "-m", type=int, default=4,
                        help="Максимально допустимое число mip-уровней (по умолчанию 4).")
    parser.add_argument("--tile-size", "-t", type=int, default=0,
                        help="Если задано (>0), считать корректными размеры, кратные этому числу (например, 16 для 16x16 тайлов).")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = args.dir if args.dir else script_dir

    zip_files = [f for f in os.listdir(target_dir) if f.lower().endswith(".zip")]
    if not zip_files:
        print("В папке нет zip-файлов:", target_dir)
        return

    print(f"Ищу .zip в: {target_dir}")
    for z in zip_files:
        zp = os.path.join(target_dir, z)
        print(f"\nПроверяю {z}...")
        npot_count, high_count, mcmeta_count, err_count = process_zip(zp, args.max_mip, args.tile_size)
        print(f"  NPOT: {npot_count}, high-mip: {high_count}, mcmeta: {mcmeta_count}, errors: {err_count}")
        print(f"  Файлы: {os.path.splitext(z)[0]}_npot.txt  ,  {os.path.splitext(z)[0]}_high_mip.txt  ,  {os.path.splitext(z)[0]}_mcmeta.txt")
        if err_count:
            print(f"  Ошибки записаны в {os.path.splitext(z)[0]}_errors.txt")

if __name__ == "__main__":
    main()

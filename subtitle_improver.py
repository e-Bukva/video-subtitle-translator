#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрипт для улучшения субтитров видео
Извлекает русскую речь через Whisper, переводит через GPT-5 и вшивает субтитры обратно
"""

import os
import sys
import re
import argparse
from pathlib import Path
from typing import List, Tuple
import tempfile
import subprocess
from datetime import datetime

# Исправляем кодировку для Windows
if sys.platform == 'win32':
    # Устанавливаем UTF-8 для stdout/stderr
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    # Альтернативный метод для старых версий Python
    else:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from openai import OpenAI, AsyncOpenAI
from dotenv import load_dotenv
import asyncio
import shutil

# Загружаем переменные из .env файла
load_dotenv()


def find_ffmpeg() -> str:
    """
    Ищет ffmpeg в разных местах на Windows
    Возвращает путь к ffmpeg.exe или 'ffmpeg' если найден в PATH
    """
    # Сначала проверяем PATH
    if shutil.which('ffmpeg'):
        return 'ffmpeg'
    
    # Список стандартных мест установки на Windows
    possible_paths = [
        r'C:\ffmpeg\bin\ffmpeg.exe',
        r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
        r'C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe',
        os.path.expanduser(r'~\ffmpeg\bin\ffmpeg.exe'),
        r'C:\ProgramData\chocolatey\bin\ffmpeg.exe',
        os.path.expanduser(r'~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe'),
        os.path.expanduser(r'~\scoop\shims\ffmpeg.exe'),
        # Локально в папке проекта
        os.path.join(os.getcwd(), 'ffmpeg', 'bin', 'ffmpeg.exe'),
        os.path.join(os.getcwd(), 'ffmpeg.exe'),
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    # Не найден
    return None


def find_ffprobe() -> str:
    """
    Ищет ffprobe в разных местах на Windows
    Возвращает путь к ffprobe.exe или 'ffprobe' если найден в PATH
    """
    # Сначала проверяем PATH
    if shutil.which('ffprobe'):
        return 'ffprobe'
    
    # Список стандартных мест установки на Windows
    possible_paths = [
        r'C:\ffmpeg\bin\ffprobe.exe',
        r'C:\Program Files\ffmpeg\bin\ffprobe.exe',
        r'C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe',
        os.path.expanduser(r'~\ffmpeg\bin\ffprobe.exe'),
        r'C:\ProgramData\chocolatey\bin\ffprobe.exe',
        os.path.expanduser(r'~\scoop\apps\ffmpeg\current\bin\ffprobe.exe'),
        os.path.expanduser(r'~\scoop\shims\ffprobe.exe'),
        # Локально в папке проекта
        os.path.join(os.getcwd(), 'ffmpeg', 'bin', 'ffprobe.exe'),
        os.path.join(os.getcwd(), 'ffprobe.exe'),
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    # Не найден
    return None


# Глобальные переменные для путей к ffmpeg
FFMPEG_PATH = None
FFPROBE_PATH = None


class SubtitleEntry:
    """Класс для работы с одной записью субтитра"""
    
    def __init__(self, index, start_time: str, end_time: str, text: str):
        self.index = index  # Может быть int или str (для суб-индексов)
        self.start_time = start_time
        self.end_time = end_time
        self.text = text
    
    def __str__(self):
        return f"{self.index}\n{self.start_time} --> {self.end_time}\n{self.text}\n"


def get_audio_duration(audio_path: str) -> float:
    """Получает длительность аудио в секундах"""
    cmd = [
        FFPROBE_PATH, '-i', audio_path,
        '-show_entries', 'format=duration',
        '-v', 'quiet',
        '-of', 'csv=p=0'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def extract_audio(video_path: str, audio_path: str) -> None:
    """Извлекает аудио из видео файла"""
    print(f"📹 Извлекаю аудио из {video_path}...")
    
    cmd = [
        FFMPEG_PATH, '-i', video_path,
        '-vn',  # без видео
        '-acodec', 'libmp3lame',  # кодек mp3
        '-ab', '128k',  # битрейт (уменьшен до 128k для экономии размера)
        '-ar', '44100',  # sample rate
        '-y',  # перезаписать если существует
        audio_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"✅ Аудио извлечено: {audio_path}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Ошибка при извлечении аудио: {e.stderr.decode()}")
        raise


def split_audio(audio_path: str, chunk_duration: int = 600) -> List[str]:
    """
    Разбивает аудио на части если файл больше 24MB
    chunk_duration: длительность части в секундах (по умолчанию 10 минут)
    Возвращает список путей к частям
    """
    file_size = os.path.getsize(audio_path)
    max_size = 24 * 1024 * 1024  # 24MB (оставляем запас)
    
    print(f"📊 Размер аудио файла: {file_size / 1024 / 1024:.2f}MB")
    
    if file_size <= max_size:
        print(f"✅ Файл помещается в лимит API, разбиение не требуется")
        return [audio_path]
    
    print(f"⚠️  Файл большой ({file_size / 1024 / 1024:.1f}MB), разбиваю на части...")
    
    duration = get_audio_duration(audio_path)
    print(f"   Длительность аудио: {duration / 60:.1f} минут")
    
    chunks = []
    base_path = audio_path.rsplit('.', 1)[0]
    extension = audio_path.rsplit('.', 1)[1]
    
    num_chunks = int(duration / chunk_duration) + 1
    
    for i in range(num_chunks):
        start_time = i * chunk_duration
        chunk_path = f"{base_path}_part{i+1}.{extension}"
        
        cmd = [
            FFMPEG_PATH, '-i', audio_path,
            '-ss', str(start_time),
            '-t', str(chunk_duration),
            '-acodec', 'copy',
            '-y',
            chunk_path
        ]
        
        subprocess.run(cmd, check=True, capture_output=True)
        chunks.append(chunk_path)
        print(f"   ✅ Создана часть {i+1}/{num_chunks}: {chunk_path}")
    
    return chunks


def split_long_subtitle_text(text: str, max_chars_per_line: int = 45) -> str:
    """
    Разбивает длинный текст субтитров на строки по границам слов
    Оптимально 45 символов на строку
    """
    # Если текст короткий - оставляем как есть
    if len(text) <= max_chars_per_line:
        return text
    
    # Разбиваем по словам
    words = text.split()
    lines = []
    current_line = []
    current_length = 0
    
    for word in words:
        word_length = len(word) + (1 if current_line else 0)  # +1 для пробела
        if current_length + word_length <= max_chars_per_line:
            current_line.append(word)
            current_length += word_length
        else:
            if current_line:
                lines.append(' '.join(current_line))
            current_line = [word]
            current_length = len(word)
    
    if current_line:
        lines.append(' '.join(current_line))
    
    return '\n'.join(lines)


def split_subtitle_entry(entry: SubtitleEntry, max_lines: int = 2) -> List[SubtitleEntry]:
    """
    Разделяет длинный SubtitleEntry на несколько коротких (максимум 2 строки каждый)
    Время делится пропорционально количеству частей
    """
    # Сначала разбиваем текст на строки по 45 символов
    text_with_lines = split_long_subtitle_text(entry.text)
    lines = text_with_lines.split('\n')
    
    # Если <= 2 строк - возвращаем как есть
    if len(lines) <= max_lines:
        entry.text = text_with_lines
        return [entry]
    
    # Делим на части по 2 строки
    parts = []
    for i in range(0, len(lines), max_lines):
        part_lines = lines[i:i + max_lines]
        parts.append('\n'.join(part_lines))
    
    # Парсим время
    def parse_time(time_str: str) -> float:
        """Конвертирует HH:MM:SS,mmm в секунды"""
        time_part = time_str.replace(',', '.')
        h, m, s = time_part.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    
    def format_time(seconds: float) -> str:
        """Конвертирует секунды в HH:MM:SS,mmm"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace('.', ',')
    
    start_sec = parse_time(entry.start_time)
    end_sec = parse_time(entry.end_time)
    duration = end_sec - start_sec
    
    # Делим время на части
    part_duration = duration / len(parts)
    
    result = []
    for i, part_text in enumerate(parts):
        part_start = start_sec + i * part_duration
        part_end = part_start + part_duration
        
        # Используем суб-индексы (например 4, 4.1, 4.2)
        sub_index = entry.index if i == 0 else f"{entry.index}_{i}"
        
        result.append(SubtitleEntry(
            index=sub_index,
            start_time=format_time(part_start),
            end_time=format_time(part_end),
            text=part_text
        ))
    
    return result


def format_timestamp(seconds: float) -> str:
    """Конвертирует секунды в формат SRT (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def transcribe_audio_chunk(audio_path: str, client: OpenAI, offset: float = 0) -> str:
    """
    Транскрибирует один файл аудио через Whisper API
    offset: смещение времени для корректировки таймингов (в секундах)
    Возвращает SRT контент
    """
    file_size = os.path.getsize(audio_path)
    print(f"   📤 Отправляю файл ({file_size / 1024 / 1024:.2f}MB) в Whisper API...", flush=True)
    
    with open(audio_path, 'rb') as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="srt",  # Классический SRT!
            language="ru"
        )
    
    print(f"   ✅ Получен ответ от Whisper API", flush=True)
    
    # Корректируем тайминги если есть смещение
    if offset > 0:
        transcript = adjust_srt_timings(transcript, offset)
    
    return transcript


def adjust_srt_timings(srt_content: str, offset_seconds: float) -> str:
    """Добавляет смещение ко всем таймингам в SRT"""
    from datetime import timedelta
    
    def add_offset(time_str: str) -> str:
        # Парсим время в формате HH:MM:SS,mmm
        h, m, s_ms = time_str.split(':')
        s, ms = s_ms.split(',')
        
        total_seconds = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        total_seconds += offset_seconds
        
        td = timedelta(seconds=total_seconds)
        hours = int(td.total_seconds() // 3600)
        minutes = int((td.total_seconds() % 3600) // 60)
        seconds = int(td.total_seconds() % 60)
        milliseconds = int((td.total_seconds() % 1) * 1000)
        
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
    
    lines = srt_content.split('\n')
    adjusted_lines = []
    
    for line in lines:
        if '-->' in line:
            start, end = line.split(' --> ')
            start = add_offset(start.strip())
            end = add_offset(end.strip())
            adjusted_lines.append(f"{start} --> {end}")
        else:
            adjusted_lines.append(line)
    
    return '\n'.join(adjusted_lines)


def transcribe_audio(audio_paths: List[str], client: OpenAI) -> str:
    """
    Транскрибирует аудио через Whisper API
    Возвращает SRT контент
    """
    if len(audio_paths) == 1:
        print(f"🎤 Транскрибирую аудио через Whisper API...", flush=True)
        transcript = transcribe_audio_chunk(audio_paths[0], client)
        print(f"✅ Транскрипция завершена", flush=True)
        return transcript
    
    print(f"🎤 Транскрибирую аудио через Whisper API ({len(audio_paths)} частей)...", flush=True)
    
    all_transcripts = []
    offset = 0
    
    for i, audio_path in enumerate(audio_paths, 1):
        print(f"   Обрабатываю часть {i}/{len(audio_paths)}...", flush=True)
        
        transcript = transcribe_audio_chunk(audio_path, client, offset)
        all_transcripts.append(transcript)
        
        # Вычисляем смещение для следующей части
        duration = get_audio_duration(audio_path)
        offset += duration
    
    # Объединяем все транскрипты
    combined = '\n\n'.join(all_transcripts)
    
    print(f"✅ Транскрипция всех частей завершена", flush=True)
    return combined


def parse_srt(srt_content: str) -> List[SubtitleEntry]:
    """Парсит SRT контент в список SubtitleEntry"""
    entries = []
    blocks = srt_content.strip().split('\n\n')
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        
        try:
            index = int(lines[0])
            timing = lines[1]
            text = '\n'.join(lines[2:])
            
            # Парсим тайминг
            match = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', timing)
            if match:
                start_time, end_time = match.groups()
                entries.append(SubtitleEntry(index, start_time, end_time, text))
        except (ValueError, IndexError):
            continue
    
    return entries


async def translate_batch_async(
    batch: List[SubtitleEntry], 
    batch_num: int, 
    total_batches: int,
    client: AsyncOpenAI, 
    model: str
) -> List[SubtitleEntry]:
    """
    Асинхронно переводит один батч субтитров
    Большие батчи (40-50) обеспечивают достаточный контекст
    """
    print(f"   Обрабатываю батч {batch_num}/{total_batches} ({len(batch)} записей)...", flush=True)
    
    # Формируем промпт с пронумерованными строками
    prompt_lines = []
    for entry in batch:
        prompt_lines.append(f"[{entry.index}] {entry.text}")
    
    system_instruction = """You are a professional subtitle translator. Translate Russian subtitles into natural, conversational English.

CRITICAL RULES:
1. Translate ALL subtitles in the batch - NEVER skip any
2. Keep [number] format for each line
3. TRANSLATE COMPLETE TEXT - do NOT shorten or cut off translations
4. Each translation must contain ALL information from the original Russian text
5. Make translations natural and conversational but COMPLETE
6. Use context from surrounding subtitles for accurate meaning
7. If original is long, translation should be long too - preserve ALL content
8. Output ONLY translations in [number] text format - one line per subtitle

Example:
Input:
[1] Добрый день, коллеги! Сегодня мы представляем вашему вниманию дизайн фитобара, расположенный на втором этаже.
[2] Начнем с планировочного решения.

Output:
[1] Good afternoon, colleagues! Today we're presenting the design for the phytobar located on the second floor.
[2] Let's start with the layout solution."""
    
    user_input = f"""<subtitles_to_translate>
{chr(10).join(prompt_lines)}
</subtitles_to_translate>

Translate all subtitles above into English. Output format: [number] translated_text"""
    
    try:
        # Используем Chat Completions API
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_input}
        ]
        
        api_params = {
            "model": model,
            "messages": messages,
            "temperature": 0.7
        }
        
        # GPT-5 требует temperature=1 и max_completion_tokens (больше т.к. reasoning тоже считается)
        if model.startswith('gpt-5') or model.startswith('o1') or model.startswith('o3'):
            api_params["temperature"] = 1.0
            api_params["max_completion_tokens"] = 16000  # Увеличено для reasoning + ответ
        else:
            api_params["max_tokens"] = 4000
        
        response = await client.chat.completions.create(**api_params)
        
        translated_text = response.choices[0].message.content
        if translated_text is None:
            translated_text = ""
        translated_text = translated_text.strip()
        
        # Парсим ответ
        translated_lines = translated_text.split('\n')
        translation_map = {}
        
        for line in translated_lines:
            match = re.match(r'\[(\d+)\]\s*(.+)', line.strip())
            if match:
                idx, text = match.groups()
                translation_map[int(idx)] = text.strip()
        
        # Создаем новые записи с переведенным текстом
        translated_batch = []
        untranslated = []
        
        for entry in batch:
            translated_text = translation_map.get(entry.index)
            
            # Проверяем что перевод получен И не содержит кириллицу
            if translated_text and not re.search(r'[А-Яа-яЁё]', translated_text):
                # Сохраняем перевод как есть (разбивка на строки будет в post-processing)
                translated_batch.append(
                    SubtitleEntry(entry.index, entry.start_time, entry.end_time, translated_text)
                )
            else:
                # Если не переведено или содержит кириллицу - оставляем оригинал и помечаем
                translated_batch.append(entry)
                untranslated.append(entry.index)
        
        if untranslated:
            print(f"   ⚠️  Батч {batch_num}: не переведено {len(untranslated)} записей: {untranslated[:5]}{'...' if len(untranslated) > 5 else ''}", flush=True)
        else:
            print(f"   ✅ Батч {batch_num}/{total_batches} завершен", flush=True)
        
        return translated_batch
    
    except Exception as e:
        print(f"   ⚠️  Ошибка в батче {batch_num}: {e}", flush=True)
        # В случае ошибки оставляем оригинальный текст
        return batch


async def translate_subtitles_async(entries: List[SubtitleEntry], api_key: str, model: str = "gpt-4o-mini") -> List[SubtitleEntry]:
    """
    Переводит и улучшает субтитры через GPT параллельно (асинхронно)
    Обрабатывает все батчи одновременно для максимальной скорости
    """
    print(f"🌐 Перевожу и улучшаю субтитры через {model} (параллельно)...", flush=True)
    
    # Создаем асинхронный клиент
    async_client = AsyncOpenAI(api_key=api_key)
    
    batch_size = 40  # Увеличили для лучшего контекста!
    total_batches = (len(entries) + batch_size - 1) // batch_size
    
    # Создаем задачи для всех батчей
    tasks = []
    for i in range(0, len(entries), batch_size):
        batch = entries[i:i + batch_size]
        batch_num = i // batch_size + 1
        task = translate_batch_async(batch, batch_num, total_batches, async_client, model)
        tasks.append(task)
    
    print(f"🚀 Запускаю {total_batches} батчей параллельно...", flush=True)
    
    # Выполняем все задачи параллельно
    results = await asyncio.gather(*tasks)
    
    # Объединяем результаты в правильном порядке
    translated_entries = []
    for batch_result in results:
        translated_entries.extend(batch_result)
    
    # POST-PROCESSING: разбиваем длинные субтитры на несколько записей (максимум 2 строки каждая)
    print(f"📐 Post-processing: разбиваю длинные субтитры на части...", flush=True)
    final_entries = []
    for entry in translated_entries:
        # Разделяем если > 2 строк, иначе просто форматируем
        parts = split_subtitle_entry(entry, max_lines=2)
        final_entries.extend(parts)
    
    translated_entries = final_entries
    
    # Проверяем сколько записей осталось непереведено
    untranslated_count = sum(1 for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text))
    
    if untranslated_count > 0:
        print(f"⚠️  ВНИМАНИЕ: {untranslated_count} записей остались непереведенными!", flush=True)
        print(f"   Запускаю повторный перевод (батчи по 10)...", flush=True)
        
        # Собираем непереведенные записи
        to_retry = [e for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text)]
        
        # Повторно переводим батчами по 30 (сохраняем контекст)
        retry_tasks = []
        for i in range(0, len(to_retry), 30):
            retry_batch = to_retry[i:i + 30]
            batch_num = i // 30 + 1
            task = translate_batch_async(retry_batch, batch_num, (len(to_retry) + 29) // 30, async_client, model)
            retry_tasks.append(task)
        
        retry_results = await asyncio.gather(*retry_tasks)
        
        # Обновляем переведенные записи
        retry_map = {}
        for batch_result in retry_results:
            for entry in batch_result:
                retry_map[entry.index] = entry
        
        # Заменяем в основном списке
        translated_entries = [retry_map.get(e.index, e) for e in translated_entries]
        
        # Проверка после первого retry
        after_retry1 = sum(1 for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text))
        
        if after_retry1 > 0:
            print(f"⚠️  После первого retry осталось {after_retry1} записей", flush=True)
            print(f"   Запускаю второй retry (батчи по 5, более агрессивно)...", flush=True)
            
            # Второй retry - батчи по 15 (последняя попытка с контекстом)
            to_retry2 = [e for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text)]
            retry_tasks2 = []
            
            for i in range(0, len(to_retry2), 15):
                retry_batch = to_retry2[i:i + 15]
                batch_num = i // 15 + 1
                task = translate_batch_async(retry_batch, batch_num, (len(to_retry2) + 14) // 15, async_client, model)
                retry_tasks2.append(task)
            
            retry_results2 = await asyncio.gather(*retry_tasks2)
            
            # Обновляем снова
            retry_map2 = {}
            for batch_result in retry_results2:
                for entry in batch_result:
                    retry_map2[entry.index] = entry
            
            translated_entries = [retry_map2.get(e.index, e) for e in translated_entries]
            
            # Финальная проверка
            final_untranslated = sum(1 for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text))
            if final_untranslated > 0:
                print(f"⚠️  После второго retry осталось {final_untranslated} непереведенных записей", flush=True)
                print(f"   Индексы: {[e.index for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text)]}", flush=True)
            else:
                print(f"✅ После второго retry все записи переведены!", flush=True)
        else:
            print(f"✅ После первого retry все записи переведены!", flush=True)
    
    print(f"✅ Перевод завершен (обработано {total_batches} батчей параллельно)", flush=True)
    return translated_entries


def translate_subtitles(entries: List[SubtitleEntry], api_key: str, model: str = "gpt-4o-mini") -> List[SubtitleEntry]:
    """
    Обертка для запуска асинхронного перевода из синхронного кода
    """
    # Для Windows: устанавливаем правильную политику event loop
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    try:
        return asyncio.run(translate_subtitles_async(entries, api_key, model))
    finally:
        # Подавляем ошибки закрытия event loop на Windows
        if sys.platform == 'win32':
            import warnings
            warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*Event loop is closed.*')


def save_srt(entries: List[SubtitleEntry], output_path: str) -> None:
    """Сохраняет субтитры в SRT файл"""
    print(f"💾 Сохраняю субтитры в {output_path}...")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(str(entry) + '\n')
    
    print(f"✅ Субтитры сохранены")


def burn_subtitles(video_path: str, srt_path: str, output_path: str) -> None:
    """Вшивает субтитры в видео (hardcoded)"""
    print(f"🎬 Вшиваю субтитры в видео...")
    
    # Конвертируем пути для ffmpeg (особенно важно для Windows)
    srt_path_escaped = srt_path.replace('\\', '/').replace(':', '\\:')
    
    cmd = [
        FFMPEG_PATH, '-i', video_path,
        '-vf', f"subtitles='{srt_path_escaped}':force_style='FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,MarginV=30'",
        '-c:a', 'copy',  # копируем аудио без перекодирования
        '-y',
        output_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"✅ Видео с субтитрами: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Ошибка при вшивании субтитров: {e.stderr.decode()}")
        raise


def main():
    parser = argparse.ArgumentParser(
        description='Улучшение субтитров для видео: транскрипция через Whisper + перевод через GPT',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python subtitle_improver.py video.mp4
  python subtitle_improver.py video.mp4 --model gpt-4o
  python subtitle_improver.py video.mp4 --skip-burn

Переменные окружения:
  OPENAI_API_KEY - ваш API ключ OpenAI (обязательно)
        """
    )
    
    parser.add_argument('video', help='Путь к видео файлу')
    parser.add_argument('--model', 
                        choices=['gpt-4o-mini', 'gpt-4o', 'gpt-5'],
                        default='gpt-4o',
                        help='Модель GPT для перевода (по умолчанию: gpt-4o, можно также задать в .env файле)')
    parser.add_argument('--skip-burn', action='store_true',
                        help='Не вшивать субтитры в видео, только создать .srt файл')
    parser.add_argument('--step', 
                        choices=['all', 'transcribe', 'translate', 'burn'],
                        default='all',
                        help='Какой этап выполнить: all (все), transcribe (только транскрипция), translate (только перевод), burn (только вшивание)')
    
    args = parser.parse_args()
    
    # Проверяем API ключ
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        print("❌ Ошибка: не установлена переменная окружения OPENAI_API_KEY")
        print()
        print("   Решение 1 (рекомендуется): Создайте .env файл")
        print("   1. Скопируйте env.example в .env")
        print("   2. Откройте .env и вставьте ваш API ключ")
        print()
        print("   Решение 2: Установите переменную окружения")
        print("   PowerShell: $env:OPENAI_API_KEY=\"your-api-key\"")
        print("   CMD:        set OPENAI_API_KEY=your-api-key")
        sys.exit(1)
    
    # Определяем модель: из аргументов, из .env или по умолчанию
    model = args.model or os.environ.get('GPT_MODEL', 'gpt-4o-mini')
    
    # Проверяем существование видео
    video_path = Path(args.video)
    if not video_path.exists():
        print(f"❌ Ошибка: файл не найден: {args.video}")
        sys.exit(1)
    
    # Инициализируем пути к ffmpeg и ffprobe (только для этапов которым они нужны)
    if args.step in ['all', 'transcribe', 'burn']:
        global FFMPEG_PATH, FFPROBE_PATH
        
        FFMPEG_PATH = find_ffmpeg()
        FFPROBE_PATH = find_ffprobe()
        
        if not FFMPEG_PATH or not FFPROBE_PATH:
            print("❌ Ошибка: ffmpeg/ffprobe не найдены!")
            print()
            print("   📥 Установите ffmpeg одним из способов:")
            print()
            print("   Способ 1 (рекомендуется): Chocolatey")
            print("   1. Установите Chocolatey: https://chocolatey.org/install")
            print("   2. Запустите PowerShell от администратора")
            print("   3. Выполните: choco install ffmpeg")
            print()
            print("   Способ 2: Scoop")
            print("   1. Установите Scoop: https://scoop.sh")
            print("   2. Выполните: scoop install ffmpeg")
            print()
            print("   Способ 3: Вручную")
            print("   1. Скачайте: https://www.gyan.dev/ffmpeg/builds/")
            print("   2. Распакуйте в C:\\ffmpeg")
            print("   3. Добавьте C:\\ffmpeg\\bin в PATH")
            print()
            print("   📍 Программа искала ffmpeg в следующих местах:")
            print("      - Системный PATH")
            print("      - C:\\ffmpeg\\bin\\")
            print("      - C:\\Program Files\\ffmpeg\\bin\\")
            print(f"      - {os.path.expanduser('~\\ffmpeg\\bin\\')}")
            print("      - C:\\ProgramData\\chocolatey\\bin\\")
            print(f"      - {os.path.expanduser('~\\scoop\\apps\\ffmpeg\\')}")
            print(f"      - {os.getcwd()} (текущая папка)")
            print()
            sys.exit(1)
        
        print(f"✅ Найден ffmpeg: {FFMPEG_PATH}")
        print(f"✅ Найден ffprobe: {FFPROBE_PATH}")
    
    # Инициализируем OpenAI клиент
    client = OpenAI(api_key=api_key)
    
    # Создаем структуру папок для выходных файлов
    base_name = video_path.stem
    outputs_dir = video_path.parent / "outputs" / base_name
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    # Русские субтитры сохраняем один раз в корне папки видео
    russian_srt = outputs_dir / "russian.srt"
    
    # Для каждого запуска создаем отдельную папку с timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = outputs_dir / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    
    output_srt = run_dir / "improved.srt"
    output_video = run_dir / "subtitled.mp4"
    
    step = args.step
    
    print(f"📁 Папка с результатами: {run_dir}", flush=True)
    
    print(f"\n{'='*60}", flush=True)
    print(f"🚀 Начинаю обработку видео: {video_path.name}", flush=True)
    print(f"{'='*60}\n", flush=True)
    
    try:
        # ========================================
        # ЭТАП 1: ТРАНСКРИПЦИЯ (Whisper)
        # ========================================
        if step in ['all', 'transcribe']:
            if russian_srt.exists():
                print(f"ℹ️  Русские субтитры уже существуют: {russian_srt}")
                print(f"   Удалите файл если хотите перетранскрибировать")
            else:
                # Шаг 1: Извлекаем аудио
                with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_audio:
                    audio_path = tmp_audio.name
                
                extract_audio(str(video_path), audio_path)
                
                # Шаг 2: Разбиваем на части если нужно
                audio_chunks = split_audio(audio_path)
                
                # Шаг 3: Транскрибируем через Whisper (возвращает SRT)
                srt_content = transcribe_audio(audio_chunks, client)
                
                # Удаляем временные аудио файлы
                for chunk in audio_chunks:
                    if os.path.exists(chunk):
                        os.unlink(chunk)
                if audio_path not in audio_chunks and os.path.exists(audio_path):
                    os.unlink(audio_path)
                
                # Парсим и сохраняем русские субтитры
                print(f"📝 Парсинг субтитров...", flush=True)
                russian_entries = parse_srt(srt_content)
                print(f"💾 Сохраняю русские субтитры в {russian_srt}...", flush=True)
                save_srt(russian_entries, str(russian_srt))
                print(f"✅ Русские субтитры сохранены: {len(russian_entries)} записей", flush=True)
        
        # Если только транскрипция - выходим
        if step == 'transcribe':
            print(f"\n{'='*60}", flush=True)
            print(f"✅ Транскрипция завершена!", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"📄 Русские субтитры: {russian_srt.relative_to(video_path.parent)}", flush=True)
            print(f"\nДля перевода запустите:", flush=True)
            print(f"  python subtitle_improver.py {args.video} --step translate\n", flush=True)
            return
        
        # ========================================
        # ЭТАП 2: ПЕРЕВОД (GPT)
        # ========================================
        if step in ['all', 'translate']:
            # Загружаем русские субтитры
            if not russian_srt.exists():
                # Пробуем найти старый формат файла (для миграции)
                old_russian_srt = video_path.parent / f"{base_name}_russian.srt"
                if old_russian_srt.exists():
                    print(f"ℹ️  Найден файл в старом формате, перемещаю в новую структуру...", flush=True)
                    russian_srt.parent.mkdir(parents=True, exist_ok=True)
                    old_russian_srt.rename(russian_srt)
                    print(f"✅ Файл перемещен в: {russian_srt.relative_to(video_path.parent)}", flush=True)
                else:
                    print(f"❌ Ошибка: файл {russian_srt} не найден")
                    print(f"   Сначала запустите: python subtitle_improver.py {args.video} --step transcribe")
                    sys.exit(1)
            
            print(f"📖 Загружаю русские субтитры из {russian_srt}...", flush=True)
            with open(russian_srt, 'r', encoding='utf-8') as f:
                srt_content = f.read()
            
            # Парсим SRT
            print(f"📝 Парсинг субтитров...", flush=True)
            russian_entries = parse_srt(srt_content)
            print(f"✅ Найдено {len(russian_entries)} записей субтитров", flush=True)
            
            # Переводим через GPT (параллельно!)
            translated_entries = translate_subtitles(russian_entries, api_key, model)
            
            # Сохраняем улучшенные субтитры
            save_srt(translated_entries, str(output_srt))
        
        # Если только перевод - выходим
        if step == 'translate':
            print(f"\n{'='*60}", flush=True)
            print(f"✅ Перевод завершен!", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"📁 Папка: {run_dir.relative_to(video_path.parent)}", flush=True)
            print(f"📄 Английские субтитры: {output_srt.name}", flush=True)
            print(f"\nДля вшивания запустите:", flush=True)
            print(f"  python subtitle_improver.py {args.video} --step burn\n", flush=True)
            return
        
        # ========================================
        # ЭТАП 3: ВШИВАНИЕ (ffmpeg)
        # ========================================
        if step in ['all', 'burn']:
            # Для burn ищем последний run с субтитрами
            if step == 'burn' and not output_srt.exists():
                # Ищем последнюю папку run_* с improved.srt
                run_dirs = sorted(outputs_dir.glob("run_*/improved.srt"))
                if run_dirs:
                    latest_srt = run_dirs[-1]
                    print(f"📖 Использую субтитры из предыдущего запуска: {latest_srt.parent.name}", flush=True)
                    # Создаем новую папку для burn
                    output_srt = latest_srt  # Используем существующие
                    output_video = latest_srt.parent / "subtitled.mp4"
                else:
                    print(f"❌ Ошибка: не найдены переведенные субтитры")
                    print(f"   Сначала запустите: python subtitle_improver.py {args.video} --step translate")
                    sys.exit(1)
            
            if args.skip_burn:
                print(f"⏭️  Пропускаю вшивание субтитров (--skip-burn)", flush=True)
            else:
                burn_subtitles(str(video_path), str(output_srt), str(output_video))
        
        # Итоги
        print(f"\n{'='*60}", flush=True)
        print(f"✅ Обработка завершена!", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"📁 Результаты сохранены в: {run_dir.relative_to(video_path.parent)}", flush=True)
        print(f"", flush=True)
        if russian_srt.exists():
            print(f"   📄 Русские субтитры: {russian_srt.relative_to(video_path.parent)}", flush=True)
        if output_srt.exists():
            print(f"   📄 Английские субтитры: {output_srt.relative_to(outputs_dir)}", flush=True)
        if not args.skip_burn and output_video.exists():
            print(f"   🎬 Видео с субтитрами: {output_video.relative_to(outputs_dir)}", flush=True)
        print(f"\n", flush=True)
        
    except KeyboardInterrupt:
        print(f"\n\n⚠️  Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()


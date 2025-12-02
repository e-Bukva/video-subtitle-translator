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


def split_audio(audio_path: str, target_chunk_size_mb: float = 12.0) -> List[str]:
    """
    Разбивает аудио на части если файл больше 24MB
    Умно вычисляет длительность чанка на основе битрейта, чтобы каждая часть была ~12MB
    target_chunk_size_mb: целевой размер чанка в МБ (по умолчанию 12MB для надёжности)
    Возвращает список путей к частям
    """
    file_size = os.path.getsize(audio_path)
    max_size = 24 * 1024 * 1024  # 24MB (лимит API)
    
    print(f"📊 Размер аудио файла: {file_size / 1024 / 1024:.2f}MB")
    
    if file_size <= max_size:
        print(f"✅ Файл помещается в лимит API, разбиение не требуется")
        return [audio_path]
    
    print(f"⚠️  Файл большой ({file_size / 1024 / 1024:.1f}MB), разбиваю на части...")
    
    duration = get_audio_duration(audio_path)
    print(f"   Длительность аудио: {duration / 60:.1f} минут")
    
    # Вычисляем битрейт (байт/секунда) и оптимальную длительность чанка
    bitrate_bytes_per_sec = file_size / duration
    target_chunk_bytes = target_chunk_size_mb * 1024 * 1024
    chunk_duration = int(target_chunk_bytes / bitrate_bytes_per_sec)
    
    # Минимум 5 минут, максимум 15 минут на чанк
    chunk_duration = max(300, min(chunk_duration, 900))
    
    print(f"   Битрейт: {bitrate_bytes_per_sec * 8 / 1000:.0f} kbps")
    print(f"   Оптимальная длительность чанка: {chunk_duration / 60:.1f} минут (~{chunk_duration * bitrate_bytes_per_sec / 1024 / 1024:.1f}MB)")
    print(f"   💡 Меньшие чанки = более надёжная обработка в Whisper API", flush=True)
    
    chunks = []
    base_path = audio_path.rsplit('.', 1)[0]
    extension = audio_path.rsplit('.', 1)[1]
    
    num_chunks = int(duration / chunk_duration) + (1 if duration % chunk_duration > 0 else 0)
    print(f"   Будет создано {num_chunks} частей")
    
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
        
        chunk_size = os.path.getsize(chunk_path)
        chunks.append(chunk_path)
        print(f"   ✅ Создана часть {i+1}/{num_chunks}: {chunk_size / 1024 / 1024:.1f}MB")
    
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
    РЕКУРСИВНАЯ разбивка: делим текст ПОПОЛАМ по словам, время ПРОПОРЦИОНАЛЬНО
    Продолжаем делить пока все части не будут <= max_lines строк
    """
    # Вспомогательные функции для работы со временем
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
    
    # Форматируем текст (разбивка на строки по 45 символов)
    text_with_lines = split_long_subtitle_text(entry.text)
    lines = text_with_lines.split('\n')
    
    # Если <= 2 строк - возвращаем как есть
    if len(lines) <= max_lines:
        entry.text = text_with_lines
        return [entry]
    
    # Если > 2 строк - делим ПОПОЛАМ (слова + время)
    words = entry.text.split()
    
    # Защита от бесконечной рекурсии: если слов слишком мало
    if len(words) < 2:
        entry.text = text_with_lines
        return [entry]
    
    # Делим слова ПОПОЛАМ
    mid = len(words) // 2
    first_half_words = words[:mid]
    second_half_words = words[mid:]
    
    # Создаем текст для каждой половины
    first_text = ' '.join(first_half_words)
    second_text = ' '.join(second_half_words)
    
    # Вычисляем время пропорционально длине текста
    start_sec = parse_time(entry.start_time)
    end_sec = parse_time(entry.end_time)
    duration = end_sec - start_sec
    
    first_len = len(first_text)
    second_len = len(second_text)
    total_len = first_len + second_len
    
    # Защита от деления на ноль
    if total_len == 0:
        entry.text = text_with_lines
        return [entry]
    
    # Время пропорционально длине текста
    first_duration = duration * (first_len / total_len)
    split_time = start_sec + first_duration
    
    # Создаем две половины
    first_entry = SubtitleEntry(
        index=entry.index,
        start_time=entry.start_time,
        end_time=format_time(split_time),
        text=first_text
    )
    
    second_entry = SubtitleEntry(
        index=f"{entry.index}_1",
        start_time=format_time(split_time),
        end_time=entry.end_time,
        text=second_text
    )
    
    # РЕКУРСИВНО обрабатываем каждую половину
    first_parts = split_subtitle_entry(first_entry, max_lines)
    second_parts = split_subtitle_entry(second_entry, max_lines)
    
    # Перенумеровываем индексы во второй половине
    # Если первая половина разбилась на N частей, вторая начинается с index_N
    if len(first_parts) > 1 or len(second_parts) > 1:
        # Обновляем индексы во второй части
        for i, part in enumerate(second_parts):
            if i == 0:
                part.index = f"{entry.index}_{len(first_parts)}"
            else:
                part.index = f"{entry.index}_{len(first_parts) + i}"
    
    # Объединяем результаты
    return first_parts + second_parts


def format_timestamp(seconds: float) -> str:
    """Конвертирует секунды в формат SRT (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def transcribe_audio_chunk(audio_path: str, client: OpenAI, offset: float = 0) -> str:
    """
    Транскрибирует один файл аудио через Whisper API (синхронно)
    offset: смещение времени для корректировки таймингов (в секундах)
    Возвращает SRT контент
    """
    file_size = os.path.getsize(audio_path)
    duration_minutes = get_audio_duration(audio_path) / 60
    estimated_time = duration_minutes * 0.3  # Примерно 18 секунд на минуту аудио
    
    print(f"   📤 Отправляю файл ({file_size / 1024 / 1024:.2f}MB, ~{duration_minutes:.1f} мин) в Whisper API...", flush=True)
    print(f"   ⏳ Ожидаемое время обработки: ~{estimated_time:.0f} секунд...", flush=True)
    print(f"   💡 Пожалуйста, подождите, Whisper обрабатывает аудио...", flush=True)
    
    with open(audio_path, 'rb') as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="srt",  # Классический SRT!
            language="ru",
            timeout=600.0  # 10 минут максимум
        )
    
    print(f"   ✅ Получен ответ от Whisper API", flush=True)
    
    # Корректируем тайминги если есть смещение
    if offset > 0:
        transcript = adjust_srt_timings(transcript, offset_seconds=offset, index_offset=0)
    
    return transcript


async def transcribe_audio_chunk_async(
    audio_path: str, 
    chunk_num: int,
    total_chunks: int,
    async_client: AsyncOpenAI, 
    offset: float = 0,
    cache_dir: str = None
) -> Tuple[int, str]:
    """
    Асинхронно транскрибирует один файл аудио через Whisper API
    Кэширует результат для быстрого повторного использования
    Возвращает кортеж (номер_чанка, SRT_контент)
    """
    import time
    start_time = time.time()
    
    # Проверяем кэш если указан cache_dir
    cache_file = None
    if cache_dir:
        cache_file = os.path.join(cache_dir, f"chunk_{chunk_num}_raw.srt")
        if os.path.exists(cache_file):
            print(f"   💾 Чанк {chunk_num}/{total_chunks}: загружаю из кэша {cache_file}", flush=True)
            with open(cache_file, 'r', encoding='utf-8') as f:
                transcript = f.read()
            
            # Корректируем тайминги если есть смещение
            if offset > 0:
                transcript = adjust_srt_timings(transcript, offset_seconds=offset, index_offset=0)
            
            print(f"   ✅ Чанк {chunk_num} загружен из кэша мгновенно!", flush=True)
            return (chunk_num, transcript)
    
    file_size = os.path.getsize(audio_path)
    duration_minutes = get_audio_duration(audio_path) / 60
    estimated_time = duration_minutes * 0.3
    
    print(f"   📤 Чанк {chunk_num}/{total_chunks}: отправляю {file_size / 1024 / 1024:.2f}MB (~{duration_minutes:.1f} мин) в Whisper API...", flush=True)
    print(f"      ⏳ Ожидаемое время: ~{estimated_time:.0f} сек", flush=True)
    
    try:
        # Читаем файл
        with open(audio_path, 'rb') as audio_file:
            file_content = audio_file.read()
        
        # Создаем file-like объект для async API
        from io import BytesIO
        audio_file_obj = BytesIO(file_content)
        audio_file_obj.name = os.path.basename(audio_path)
        
        # Оборачиваем в asyncio.wait_for для контроля таймаута
        async def do_transcription():
            return await async_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file_obj,
                response_format="srt",
                language="ru",
                timeout=1200.0
            )
        
        # Ждем с таймаутом 25 минут (максимум для чанка)
        transcript = await asyncio.wait_for(do_transcription(), timeout=1500.0)
        
        elapsed_time = time.time() - start_time
        print(f"   ✅ Чанк {chunk_num}/{total_chunks} завершен успешно за {elapsed_time:.0f} сек!", flush=True)
        
        # СОХРАНЯЕМ В КЭШ перед корректировкой (сохраняем raw без offset)
        if cache_file:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, 'w', encoding='utf-8') as f:
                f.write(transcript)
            print(f"   💾 Чанк {chunk_num} сохранен в кэш: {cache_file}", flush=True)
        
        # Корректируем тайминги если есть смещение (индексы перенумеруем позже при объединении)
        if offset > 0:
            transcript = adjust_srt_timings(transcript, offset_seconds=offset, index_offset=0)
        
        return (chunk_num, transcript)
    
    except asyncio.TimeoutError:
        print(f"   ❌ Чанк {chunk_num}/{total_chunks} превысил таймаут (25 минут)!", flush=True)
        print(f"   💡 Возможно Whisper API перегружен, попробуйте позже", flush=True)
        raise Exception(f"Timeout при обработке чанка {chunk_num}")
    except Exception as e:
        print(f"   ❌ Ошибка в чанке {chunk_num}/{total_chunks}: {type(e).__name__}: {e}", flush=True)
        raise


def adjust_srt_timings(srt_content: str, offset_seconds: float, index_offset: int = 0) -> str:
    """
    Добавляет смещение ко всем таймингам в SRT и перенумеровывает индексы
    offset_seconds: смещение времени в секундах
    index_offset: смещение для индексов субтитров (для правильной нумерации при склейке)
    """
    from datetime import timedelta
    
    def add_offset(time_str: str) -> str:
        # Парсим время в формате HH:MM:SS,mmm
        h, m, s_ms = time_str.split(':')
        s, ms = s_ms.split(',')
        
        # Более точная конвертация с float
        total_seconds = float(h) * 3600 + float(m) * 60 + float(s) + float(ms) / 1000.0
        total_seconds += offset_seconds
        
        # Разбиваем на компоненты с правильным округлением
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = total_seconds % 60
        secs = int(seconds)
        milliseconds = int(round((seconds - secs) * 1000))
        
        # Обработка случая когда миллисекунды округлились до 1000
        if milliseconds >= 1000:
            milliseconds = 0
            secs += 1
        
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"
    
    lines = srt_content.split('\n')
    adjusted_lines = []
    
    for line in lines:
        line_stripped = line.strip()
        
        # Проверяем, это индекс субтитра (только цифры)
        if line_stripped.isdigit():
            # Перенумеровываем индекс
            new_index = int(line_stripped) + index_offset
            adjusted_lines.append(str(new_index))
        elif '-->' in line:
            # Корректируем тайминг
            start, end = line.split(' --> ')
            start = add_offset(start.strip())
            end = add_offset(end.strip())
            adjusted_lines.append(f"{start} --> {end}")
        else:
            # Остальное переносим как есть (включая пустые строки!)
            adjusted_lines.append(line)
    
    return '\n'.join(adjusted_lines)


async def transcribe_audio_async(audio_paths: List[str], api_key: str, sequential: bool = True, cache_dir: str = None) -> str:
    """
    Асинхронно транскрибирует аудио через Whisper API
    sequential: если True - обрабатывает последовательно (надежнее), если False - параллельно (быстрее но нестабильно)
    cache_dir: папка для кэширования чанков (чтобы не транскрибировать повторно)
    Возвращает SRT контент
    """
    if len(audio_paths) == 1:
        # Один файл - используем синхронную версию (проще)
        print(f"🎤 Транскрибирую аудио через Whisper API...", flush=True)
        sync_client = OpenAI(api_key=api_key, timeout=1200.0)
        transcript = transcribe_audio_chunk(audio_paths[0], sync_client)
        print(f"✅ Транскрипция завершена", flush=True)
        return transcript
    
    if sequential:
        # ПОСЛЕДОВАТЕЛЬНАЯ ОБРАБОТКА - надежнее для Whisper API
        print(f"🎤 Транскрибирую аудио через Whisper API ({len(audio_paths)} частей ПОСЛЕДОВАТЕЛЬНО)...", flush=True)
        print(f"   💡 Последовательная обработка надежнее для больших файлов", flush=True)
        
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            print(f"   💾 Кэш чанков: {cache_dir}", flush=True)
        
        async_client = AsyncOpenAI(api_key=api_key, timeout=1200.0)
        
        # Вычисляем смещения для каждого чанка
        offsets = [0]
        for audio_path in audio_paths[:-1]:
            duration = get_audio_duration(audio_path)
            offsets.append(offsets[-1] + duration)
        
        all_transcripts = []
        
        for i, (audio_path, offset) in enumerate(zip(audio_paths, offsets), 1):
            print(f"\n📍 Обрабатываю чанк {i}/{len(audio_paths)}...", flush=True)
            
            # Пауза перед отправкой (кроме первого чанка) чтобы не перегружать API
            if i > 1:
                pause_seconds = 15
                print(f"   ⏸️  Пауза {pause_seconds} сек перед отправкой (чтобы не перегружать Whisper API)...", flush=True)
                await asyncio.sleep(pause_seconds)
            
            try:
                chunk_num, transcript = await transcribe_audio_chunk_async(
                    audio_path=audio_path,
                    chunk_num=i,
                    total_chunks=len(audio_paths),
                    async_client=async_client,
                    offset=offset,
                    cache_dir=cache_dir
                )
                all_transcripts.append((chunk_num, transcript))
                print(f"   ✅ Чанк {i} успешно обработан и сохранен!", flush=True)
                
            except Exception as e:
                print(f"   ❌ Ошибка при обработке чанка {i}: {e}", flush=True)
                print(f"   🔄 Пробую еще раз через 20 секунд...", flush=True)
                
                # Retry один раз с большей паузой
                try:
                    await asyncio.sleep(20)
                    print(f"   📤 Повторная отправка чанка {i}...", flush=True)
                    chunk_num, transcript = await transcribe_audio_chunk_async(
                        audio_path=audio_path,
                        chunk_num=i,
                        total_chunks=len(audio_paths),
                        async_client=async_client,
                        offset=offset
                    )
                    all_transcripts.append((chunk_num, transcript))
                    print(f"   ✅ Чанк {i} обработан после retry!", flush=True)
                except Exception as e2:
                    print(f"   ❌ Повторная попытка тоже не удалась: {e2}", flush=True)
                    print(f"   ⚠️  Пропускаю этот чанк, продолжаю со следующим...", flush=True)
                    continue
        
        if not all_transcripts:
            raise Exception("Все чанки завершились с ошибкой!")
        
        # Объединяем результаты
        print(f"\n🔗 Объединяю {len(all_transcripts)} успешных чанков...", flush=True)
        
        all_transcripts.sort(key=lambda x: x[0])
        index_offset = 0
        combined_transcripts = []
        
        for chunk_num, transcript in all_transcripts:
            subtitle_count = transcript.strip().count('\n\n') + 1 if transcript.strip() else 0
            
            if chunk_num > 1 and index_offset > 0:
                adjusted_transcript = adjust_srt_timings(transcript, offset_seconds=0, index_offset=index_offset)
            else:
                adjusted_transcript = transcript
            
            index_offset += subtitle_count
            combined_transcripts.append(adjusted_transcript)
            print(f"   ✓ Чанк {chunk_num}: {subtitle_count} субтитров", flush=True)
        
        combined = '\n\n'.join(combined_transcripts)
        print(f"✅ Транскрипция всех {len(audio_paths)} частей завершена (всего {index_offset} субтитров)!", flush=True)
        return combined
    
    # ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА (может быть нестабильна для больших файлов)
    print(f"🎤 Транскрибирую аудио через Whisper API ({len(audio_paths)} частей ПАРАЛЛЕЛЬНО)...", flush=True)
    
    # Создаем асинхронный клиент
    async_client = AsyncOpenAI(api_key=api_key, timeout=1200.0)
    
    # Вычисляем смещения для каждого чанка
    offsets = [0]
    for audio_path in audio_paths[:-1]:
        duration = get_audio_duration(audio_path)
        offsets.append(offsets[-1] + duration)
    
    # Создаем задачи для всех чанков
    tasks = []
    for i, (audio_path, offset) in enumerate(zip(audio_paths, offsets), 1):
        task = transcribe_audio_chunk_async(
            audio_path=audio_path,
            chunk_num=i,
            total_chunks=len(audio_paths),
            async_client=async_client,
            offset=offset
        )
        tasks.append(task)
    
    print(f"🚀 Запускаю транскрипцию {len(audio_paths)} чанков одновременно...", flush=True)
    print(f"   💡 Все чанки обрабатываются параллельно - это намного быстрее!", flush=True)
    
    # Выполняем все задачи параллельно с обработкой частичных ошибок
    # Используем return_exceptions=True чтобы получить все результаты (включая ошибки)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Разделяем на успешные и неудачные
    successful_results = []
    failed_chunks = []
    
    for i, result in enumerate(results, 1):
        if isinstance(result, Exception):
            failed_chunks.append(i)
            print(f"   ❌ Чанк {i} завершился с ошибкой: {type(result).__name__}: {result}", flush=True)
        else:
            successful_results.append(result)
    
    # Проверяем что хоть что-то получилось
    if not successful_results:
        print(f"\n❌ Все {len(audio_paths)} чанков завершились с ошибкой!", flush=True)
        raise Exception("Все чанки завершились с ошибкой! Проверьте подключение к API или попробуйте позже.")
    
    # Если есть неудачные - предупреждаем
    if failed_chunks:
        print(f"\n⚠️  ВНИМАНИЕ: {len(failed_chunks)} из {len(audio_paths)} чанков не обработано: {failed_chunks}", flush=True)
        print(f"   Продолжаю с {len(successful_results)} успешными чанками...", flush=True)
        print(f"   💡 Субтитры будут неполными - можно запустить повторно позже", flush=True)
    
    # Перенумеровываем индексы субтитров при объединении
    print(f"🔗 Объединяю {len(successful_results)} чанков с правильной нумерацией субтитров...", flush=True)
    
    # Сортируем по номеру чанка
    successful_results.sort(key=lambda x: x[0])
    
    all_transcripts = []
    index_offset = 0
    
    for chunk_num, transcript in successful_results:
        # Подсчитываем количество субтитров в этом чанке
        subtitle_count = transcript.strip().count('\n\n') + 1 if transcript.strip() else 0
        
        # Перенумеровываем индексы начиная с offset (для чанков после первого)
        if chunk_num > 1 and index_offset > 0:
            adjusted_transcript = adjust_srt_timings(transcript, offset_seconds=0, index_offset=index_offset)
        else:
            adjusted_transcript = transcript
        
        index_offset += subtitle_count
        all_transcripts.append(adjusted_transcript)
        print(f"   ✓ Чанк {chunk_num}: {subtitle_count} субтитров", flush=True)
    
    combined = '\n\n'.join(all_transcripts)
    
    if failed_chunks:
        print(f"⚠️  Транскрипция завершена ЧАСТИЧНО: {len(successful_results)}/{len(audio_paths)} чанков (всего {index_offset} субтитров)", flush=True)
    else:
        print(f"✅ Транскрипция всех {len(audio_paths)} частей завершена (всего {index_offset} субтитров)!", flush=True)
    
    return combined


def transcribe_audio(audio_paths: List[str], client: OpenAI, cache_dir: str = None) -> str:
    """
    Транскрибирует аудио через Whisper API
    Обертка для запуска асинхронной версии из синхронного кода
    cache_dir: папка для кэширования чанков
    Возвращает SRT контент
    """
    # Получаем API ключ из клиента
    api_key = client.api_key
    
    # Для Windows: устанавливаем правильную политику event loop
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    try:
        return asyncio.run(transcribe_audio_async(audio_paths, api_key, cache_dir=cache_dir))
    finally:
        # Подавляем ошибки закрытия event loop на Windows
        if sys.platform == 'win32':
            import warnings
            warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*Event loop is closed.*')


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
6. IMPORTANT: Adjacent entries may be parts of ONE sentence split by timing - read context from neighbors to understand full meaning
7. But OUTPUT must have SAME number of [number] lines as input - one translation per entry
8. If an entry starts mid-sentence (no capital letter, continues from previous), translate it naturally while keeping same boundary split
9. Output ONLY translations in [number] text format - one line per subtitle

Example with split sentence:
Input:
[1] чтобы можно
[2] было присесть в перерывы, да, но это все равно

Output:
[1] so you can
[2] sit during breaks, yes, but it's still

Example with complete sentence:
[1] Добрый день, коллеги! Сегодня мы представляем дизайн фитобара.
[2] Начнем с планировочного решения.

Output:
[1] Good afternoon, colleagues! Today we're presenting the phytobar design.
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
    
    # ПРОВЕРКА КАЧЕСТВА: сначала убедимся что все переведено, ПОТОМ делаем post-processing
    print(f"🔍 Проверяю качество перевода...", flush=True)
    untranslated_count = sum(1 for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text))
    
    # RETRY LOGIC - делаем ДО post-processing чтобы индексы не менялись
    retry_round = 0
    max_retries = 2
    
    while untranslated_count > 0 and retry_round < max_retries:
        retry_round += 1
        print(f"\n⚠️  Обнаружено {untranslated_count} непереведенных записей", flush=True)
        
        # Собираем непереведенные записи с их индексами
        to_retry = [e for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text)]
        untranslated_indices = [e.index for e in to_retry]
        print(f"   📋 Индексы: {untranslated_indices[:10]}{'...' if len(untranslated_indices) > 10 else ''}", flush=True)
        print(f"   🔄 Запускаю retry #{retry_round}/{max_retries} (батчи по 20)...", flush=True)
        
        # Повторно переводим меньшими батчами для лучшей концентрации
        retry_batch_size = 20
        retry_tasks = []
        for i in range(0, len(to_retry), retry_batch_size):
            retry_batch = to_retry[i:i + retry_batch_size]
            batch_num = i // retry_batch_size + 1
            total_retry_batches = (len(to_retry) + retry_batch_size - 1) // retry_batch_size
            task = translate_batch_async(retry_batch, batch_num, total_retry_batches, async_client, model)
            retry_tasks.append(task)
        
        retry_results = await asyncio.gather(*retry_tasks)
        
        # Создаем карту переведенных записей по индексу
        retry_map = {}
        for batch_result in retry_results:
            for entry in batch_result:
                # Проверяем что действительно переведено (без кириллицы)
                if not re.search(r'[А-Яа-яЁё]', entry.text):
                    retry_map[entry.index] = entry
        
        # Заменяем в основном списке только успешно переведенные
        updated_entries = []
        for entry in translated_entries:
            if entry.index in retry_map:
                updated_entries.append(retry_map[entry.index])
            else:
                updated_entries.append(entry)
        translated_entries = updated_entries
        
        # Проверяем прогресс
        prev_untranslated = untranslated_count
        untranslated_count = sum(1 for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text))
        
        if untranslated_count == 0:
            print(f"   ✅ Все записи успешно переведены после retry #{retry_round}!", flush=True)
            break
        elif untranslated_count < prev_untranslated:
            print(f"   📉 Прогресс: {prev_untranslated} → {untranslated_count} непереведенных", flush=True)
        else:
            print(f"   ⚠️  Нет прогресса: все еще {untranslated_count} непереведенных", flush=True)
    
    # Финальная проверка
    final_untranslated = sum(1 for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text))
    if final_untranslated > 0:
        print(f"\n⚠️  ВНИМАНИЕ: {final_untranslated} записей остались непереведенными после {retry_round} retry", flush=True)
        failed_indices = [e.index for e in translated_entries if re.search(r'[А-Яа-яЁё]', e.text)]
        print(f"   Непереведенные индексы: {failed_indices}", flush=True)
        print(f"   💡 Эти записи сохранятся на русском языке", flush=True)
    else:
        print(f"✅ Все записи успешно переведены!", flush=True)
    
    # POST-PROCESSING: разбиваем длинные английские субтитры (>2 строк)
    # Делим слова пополам + время пропорционально (рекурсивно пока <= 2 строк)
    print(f"\n📐 Post-processing: разбиваю длинные субтитры на части (макс 2 строки)...", flush=True)
    final_entries = []
    for entry in translated_entries:
        # Рекурсивно делим если > 2 строк
        parts = split_subtitle_entry(entry, max_lines=2)
        final_entries.extend(parts)
    
    print(f"✅ Перевод завершен: {len(entries)} → {len(final_entries)} записей (после разбиения длинных)", flush=True)
    return final_entries


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
        '-vf', f"subtitles='{srt_path_escaped}':force_style='FontName=Arial,FontSize=12,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80404040,BorderStyle=3,Outline=1,Shadow=0,MarginV=10'",
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
    parser.add_argument('--force-retranscribe', action='store_true',
                        help='Игнорировать кэш чанков и транскрибировать заново')
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
    
    # Инициализируем OpenAI клиент с увеличенным таймаутом
    client = OpenAI(api_key=api_key, timeout=1200.0)  # 20 минут для больших файлов
    
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
                
                # Создаем папку для кэша чанков
                chunks_cache_dir = outputs_dir / "chunks"
                
                # Если --force-retranscribe, удаляем старый кэш
                if args.force_retranscribe and chunks_cache_dir.exists():
                    import shutil
                    shutil.rmtree(chunks_cache_dir)
                    print(f"🗑️  Удален старый кэш чанков, будет выполнена полная транскрибация", flush=True)
                
                # Шаг 3: Транскрибируем через Whisper (возвращает SRT)
                # Кэшируем чанки чтобы не транскрибировать повторно
                srt_content = transcribe_audio(audio_chunks, client, cache_dir=str(chunks_cache_dir))
                
                # Удаляем временные аудио файлы
                for chunk in audio_chunks:
                    if os.path.exists(chunk):
                        os.unlink(chunk)
                if audio_path not in audio_chunks and os.path.exists(audio_path):
                    os.unlink(audio_path)
                
                # Debug: сохраняем RAW SRT во временный файл для анализа
                temp_raw_path = str(russian_srt).replace('.srt', '_raw_debug.srt')
                with open(temp_raw_path, 'w', encoding='utf-8') as f:
                    f.write(srt_content)
                print(f"🐛 Debug: RAW SRT сохранен в {temp_raw_path}", flush=True)
                
                # Парсим и сохраняем русские субтитры
                print(f"📝 Парсинг субтитров...", flush=True)
                
                # Debug: сколько блоков в raw SRT
                raw_blocks = srt_content.strip().split('\n\n')
                print(f"   🔍 Raw SRT: {len(raw_blocks)} блоков, {len(srt_content)} символов", flush=True)
                
                russian_entries = parse_srt(srt_content)
                print(f"   ✅ Распарсено: {len(russian_entries)} записей из {len(raw_blocks)} блоков", flush=True)
                
                if len(russian_entries) < len(raw_blocks):
                    print(f"   ⚠️  ВНИМАНИЕ: Потеряно {len(raw_blocks) - len(russian_entries)} блоков при парсинге!", flush=True)
                
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


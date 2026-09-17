#!/usr/bin/env python3
"""
Скрипт для конвертации RTF файла в Markdown и подготовки чанков 
с Parent-Child структурой для Dify (модель встраивания bge-m3).

bge-m3 характеристики:
- Максимальная длина контекста: 8192 токенов
- Рекомендуемый размер чанка: 512-1024 токенов для дочерних чанков
- Поддерживает мультиязычность (включая русский)

Parent-Child стратегия:
- Parent chunks: 1500-2000 символов (контекст для понимания)
- Child chunks: 300-500 символов (для точного поиска)
"""

import re
import json
from pathlib import Path
from typing import List, Dict, Any
from striprtf.striprtf import rtf_to_text


def read_rtf_file(rtf_path: str) -> str:
    """Чтение и конвертация RTF файла в текст."""
    with open(rtf_path, 'rb') as f:
        content = f.read().decode('cp1251', errors='ignore')
    return rtf_to_text(content)


def clean_text(text: str) -> str:
    """Очистка текста от артефактов RTF и лишних пробелов."""
    # Удаление множественных пробелов и табуляций
    text = re.sub(r'[ \t]+', ' ', text)
    # Удаление лишних пустых строк (оставляем максимум 2 подряд)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Замена специальных символов
    text = text.replace('\u00a0', ' ')  # неразрывный пробел
    text = text.replace('\ufeff', '')   # BOM
    # Удаление ведущих/замыкающих пробелов на каждой строке
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    return text.strip()


def extract_sections(text: str) -> List[Dict[str, Any]]:
    """
    Выделение логических секций из текста.
    Возвращает список секций с заголовками и содержанием.
    """
    sections = []
    
    # Паттерны для обнаружения заголовков
    # Заголовок уровня 1: полностью ЗАГЛАВНЫЕ буквы или номер раздела
    heading_patterns = [
        r'^(РЕШЕНИЕ|ПОСТАНОВЛЕНИЕ|ПРИКАЗ|ГОСУДАРСТВЕННАЯ КОМИССИЯ).*$',
        r'^от \d+ [а-я]+ \d{4} г\.? N',
        r'^О [\w\s]+$',
        r'^\d+\.\s+[А-Я][\w\s]+$',  # Нумерованные разделы
        r'^[IVX]+\.\s+[А-Я][\w\s]+$',  # Римские цифры
        r'^Приложение\s+\d+',
        r'^Таблица\s+\d+',
    ]
    
    lines = text.split('\n')
    current_section = {
        'title': '',
        'content': [],
        'level': 0,
        'start_line': 0
    }
    
    for i, line in enumerate(lines):
        is_heading = False
        title = ''
        level = 1
        
        for pattern in heading_patterns:
            if re.match(pattern, line.strip(), re.IGNORECASE):
                is_heading = True
                title = line.strip()
                # Определение уровня заголовка
                if re.match(r'^(РЕШЕНИЕ|ПОСТАНОВЛЕНИЕ|ПРИКАЗ)', line.strip()):
                    level = 0
                elif re.match(r'^\d+\.', line.strip()):
                    level = 1
                elif re.match(r'^[IVX]+\.', line.strip()):
                    level = 1
                else:
                    level = 2
                break
        
        if is_heading and len(line.strip()) > 5:
            # Сохраняем предыдущую секцию
            if current_section['content']:
                sections.append({
                    'title': current_section['title'],
                    'content': '\n'.join(current_section['content']).strip(),
                    'level': current_section['level'],
                    'start_line': current_section['start_line'],
                    'end_line': i - 1
                })
            
            # Начинаем новую секцию
            current_section = {
                'title': title,
                'content': [],
                'level': level,
                'start_line': i
            }
        else:
            if line.strip():
                current_section['content'].append(line.strip())
    
    # Добавляем последнюю секцию
    if current_section['content']:
        sections.append({
            'title': current_section['title'],
            'content': '\n'.join(current_section['content']).strip(),
            'level': current_section['level'],
            'start_line': current_section['start_line'],
            'end_line': len(lines)
        })
    
    return sections


def estimate_tokens(text: str) -> int:
    """
    Грубая оценка количества токенов.
    Для русского языка ~1 токен на 3-4 символа.
    """
    return len(text) // 3


def create_parent_child_chunks(
    sections: List[Dict[str, Any]],
    parent_max_chars: int = 2000,
    child_max_chars: int = 500,
    overlap_chars: int = 100
) -> List[Dict[str, Any]]:
    """
    Создание Parent-Child чанков.
    
    Parent chunk: содержит контекст (заголовок + содержание секции)
    Child chunk: меньшие фрагменты для точного поиска
    
    Параметры оптимизированы для bge-m3:
    - parent_max_chars: ~2000 символов (~600-700 токенов)
    - child_max_chars: ~500 символов (~150-170 токенов)
    """
    all_chunks = []
    chunk_id = 0
    
    for section_idx, section in enumerate(sections):
        section_title = section['title']
        section_content = section['content']
        
        if not section_content:
            continue
        
        # Создаем полный текст секции с заголовком
        full_section_text = f"{section_title}\n{section_content}" if section_title else section_content
        
        # Создаем parent chunk для всей секции (если она не слишком большая)
        if len(full_section_text) <= parent_max_chars * 1.5:
            parent_id = f"parent_{chunk_id}"
            parent_chunk = {
                'id': parent_id,
                'type': 'parent',
                'content': full_section_text,
                'metadata': {
                    'section_index': section_idx,
                    'title': section_title,
                    'level': section.get('level', 0),
                    'char_count': len(full_section_text),
                    'token_estimate': estimate_tokens(full_section_text),
                    'source': 'gkrch2023.rtf'
                },
                'child_ids': []
            }
            
            # Разбиваем содержание на child chunks
            child_chunks = split_into_child_chunks(
                section_content,
                child_max_chars,
                overlap_chars,
                section_title
            )
            
            child_ids = []
            for child in child_chunks:
                child_id = f"child_{chunk_id}_{len(child_ids)}"
                child['id'] = child_id
                child['parent_id'] = parent_id
                child_ids.append(child_id)
                all_chunks.append(child)
            
            parent_chunk['child_ids'] = child_ids
            all_chunks.insert(0, parent_chunk)  # Parent перед children
            chunk_id += 1
        else:
            # Если секция очень большая, разбиваем на несколько parent-child групп
            large_parent_chunks = split_into_parent_chunks(
                full_section_text,
                parent_max_chars,
                overlap_chars,
                section_title
            )
            
            for lp_idx, lp_text in enumerate(large_parent_chunks):
                parent_id = f"parent_{chunk_id}_{lp_idx}"
                parent_chunk = {
                    'id': parent_id,
                    'type': 'parent',
                    'content': lp_text,
                    'metadata': {
                        'section_index': section_idx,
                        'part_index': lp_idx,
                        'title': section_title,
                        'level': section.get('level', 0),
                        'char_count': len(lp_text),
                        'token_estimate': estimate_tokens(lp_text),
                        'source': 'gkrch2023.rtf'
                    },
                    'child_ids': []
                }
                
                # Создаем child chunks внутри этого parent
                child_chunks = split_into_child_chunks(
                    lp_text,
                    child_max_chars,
                    overlap_chars,
                    section_title
                )
                
                child_ids = []
                for child in child_chunks:
                    child_id = f"child_{chunk_id}_{lp_idx}_{len(child_ids)}"
                    child['id'] = child_id
                    child['parent_id'] = parent_id
                    child_ids.append(child_id)
                    all_chunks.append(child)
                
                parent_chunk['child_ids'] = child_ids
                all_chunks.append(parent_chunk)
                chunk_id += 1
    
    return all_chunks


def split_into_parent_chunks(
    text: str,
    max_chars: int,
    overlap: int,
    title: str = ''
) -> List[str]:
    """Разбиение большого текста на parent chunks с перекрытием."""
    chunks = []
    start = 0
    
    while start < len(text):
        end = min(start + max_chars, len(text))
        
        # Пытаемся разбить по предложению или абзацу
        if end < len(text):
            # Ищем ближайший разрыв строки или точку
            break_point = text.rfind('\n', start, end)
            if break_point == -1 or break_point < start + max_chars // 2:
                break_point = text.rfind('. ', start, end)
            if break_point != -1 and break_point > start + max_chars // 2:
                end = break_point + 1
        
        chunk_text = text[start:end].strip()
        if title and not chunk_text.startswith(title):
            chunk_text = f"{title}\n{chunk_text}"
        
        if chunk_text:
            chunks.append(chunk_text)
        
        start = end - overlap if end < len(text) else len(text)
    
    return chunks


def split_into_child_chunks(
    text: str,
    max_chars: int,
    overlap: int,
    title: str = ''
) -> List[Dict[str, Any]]:
    """Разбиение текста на child chunks с перекрытием."""
    chunks = []
    start = 0
    
    # Сначала пытаемся разбить по абзацам
    paragraphs = [p for p in text.split('\n\n') if p.strip()]
    
    current_chunk = ''
    current_start = 0
    
    for para in paragraphs:
        if len(current_chunk) + len(para) <= max_chars:
            if current_chunk:
                current_chunk += '\n\n' + para
            else:
                current_chunk = para
                current_start = start
            start += len(para) + 2
        else:
            if current_chunk:
                chunks.append({
                    'type': 'child',
                    'content': current_chunk,
                    'metadata': {
                        'char_count': len(current_chunk),
                        'token_estimate': estimate_tokens(current_chunk)
                    }
                })
            current_chunk = para
            current_start = start
            start += len(para) + 2
    
    if current_chunk:
        chunks.append({
            'type': 'child',
            'content': current_chunk,
            'metadata': {
                'char_count': len(current_chunk),
                'token_estimate': estimate_tokens(current_chunk)
            }
        })
    
    # Если chunks слишком большие, дополнительно разбиваем
    final_chunks = []
    for chunk in chunks:
        if len(chunk['content']) > max_chars * 1.2:
            sub_chunks = split_by_sentences(chunk['content'], max_chars, overlap, title)
            final_chunks.extend(sub_chunks)
        else:
            final_chunks.append(chunk)
    
    return final_chunks


def split_by_sentences(
    text: str,
    max_chars: int,
    overlap: int,
    title: str = ''
) -> List[Dict[str, Any]]:
    """Разбиение текста по предложениям для очень больших chunks."""
    # Простое разбиение по предложениям
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = ''
    
    for sentence in sentences:
        if len(current_chunk) + len(sentence) <= max_chars:
            current_chunk += (' ' if current_chunk else '') + sentence
        else:
            if current_chunk:
                if title and not current_chunk.startswith(title):
                    current_chunk = f"{title[:100]}\n{current_chunk}"
                chunks.append({
                    'type': 'child',
                    'content': current_chunk.strip(),
                    'metadata': {
                        'char_count': len(current_chunk),
                        'token_estimate': estimate_tokens(current_chunk)
                    }
                })
            current_chunk = sentence
    
    if current_chunk:
        if title and not current_chunk.startswith(title):
            current_chunk = f"{title[:100]}\n{current_chunk}"
        chunks.append({
            'type': 'child',
            'content': current_chunk.strip(),
            'metadata': {
                'char_count': len(current_chunk),
                'token_estimate': estimate_tokens(current_chunk)
            }
        })
    
    return chunks


def convert_to_markdown(sections: List[Dict[str, Any]]) -> str:
    """Конвертация секций в Markdown формат."""
    md_lines = []
    
    for section in sections:
        title = section['title']
        content = section['content']
        level = section.get('level', 1)
        
        if title:
            # Добавляем заголовок соответствующего уровня
            hash_count = min(level + 1, 6)  # Максимум 6 уровней
            md_lines.append(f"{'#' * hash_count} {title}")
        
        if content:
            md_lines.append(content)
        
        md_lines.append('')  # Пустая строка между секциями
    
    return '\n'.join(md_lines)


def export_for_dify(chunks: List[Dict[str, Any]], output_format: str = 'json') -> str:
    """
    Экспорт чанков в формате для импорта в Dify.
    
    Поддерживаемые форматы:
    - json: Полный JSON с метаданными
    - csv: CSV для bulk import
    - txt: Простой текстовый формат
    """
    if output_format == 'json':
        # Формат для Dify API или bulk import
        dify_data = {
            'document_name': 'gkrch2023',
            'embedding_model': 'bge-m3',
            'chunk_strategy': 'parent_child',
            'chunks': []
        }
        
        for chunk in chunks:
            dify_chunk = {
                'content': chunk['content'],
                'metadata': chunk.get('metadata', {}),
                'type': chunk.get('type', 'child')
            }
            if 'parent_id' in chunk:
                dify_chunk['parent_id'] = chunk['parent_id']
            if 'child_ids' in chunk:
                dify_chunk['child_ids'] = chunk['child_ids']
            
            dify_data['chunks'].append(dify_chunk)
        
        return json.dumps(dify_data, ensure_ascii=False, indent=2)
    
    elif output_format == 'csv':
        import csv
        import io
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['content', 'metadata', 'type', 'parent_id', 'id'])
        
        for chunk in chunks:
            metadata_json = json.dumps(chunk.get('metadata', {}), ensure_ascii=False)
            parent_id = chunk.get('parent_id', '')
            chunk_id = chunk.get('id', '')
            chunk_type = chunk.get('type', 'child')
            writer.writerow([chunk['content'], metadata_json, chunk_type, parent_id, chunk_id])
        
        return output.getvalue()
    
    else:  # txt format
        lines = []
        for chunk in chunks:
            lines.append(f"=== {chunk.get('id', 'N/A')} ({chunk.get('type', 'unknown')}) ===")
            lines.append(chunk['content'])
            lines.append(f"Metadata: {json.dumps(chunk.get('metadata', {}), ensure_ascii=False)}")
            lines.append('')
        return '\n'.join(lines)


def main():
    """Основная функция обработки."""
    # Пути к файлам
    rtf_file = '/workspace/gkrch2023.rtf'
    output_dir = Path('/workspace/output')
    output_dir.mkdir(exist_ok=True)
    
    print("=== Конвертация RTF в Markdown и подготовка чанков для Dify ===\n")
    
    # Шаг 1: Чтение и конвертация RTF
    print("1. Чтение RTF файла...")
    raw_text = read_rtf_file(rtf_file)
    print(f"   Размер сырого текста: {len(raw_text)} символов")
    
    # Шаг 2: Очистка текста
    print("\n2. Очистка текста...")
    cleaned_text = clean_text(raw_text)
    print(f"   Размер очищенного текста: {len(cleaned_text)} символов")
    
    # Шаг 3: Выделение секций
    print("\n3. Выделение логических секций...")
    sections = extract_sections(cleaned_text)
    print(f"   Найдено секций: {len(sections)}")
    
    # Шаг 4: Конвертация в Markdown
    print("\n4. Конвертация в Markdown...")
    markdown_content = convert_to_markdown(sections)
    md_output_path = output_dir / 'gkrch2023.md'
    with open(md_output_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)
    print(f"   Markdown сохранен: {md_output_path}")
    
    # Шаг 5: Создание Parent-Child чанков
    print("\n5. Создание Parent-Child чанков...")
    print("   Параметры:")
    print("   - Parent max: 2000 символов (~600-700 токенов)")
    print("   - Child max: 500 символов (~150-170 токенов)")
    print("   - Overlap: 100 символов")
    
    chunks = create_parent_child_chunks(
        sections,
        parent_max_chars=2000,
        child_max_chars=500,
        overlap_chars=100
    )
    
    parent_count = sum(1 for c in chunks if c.get('type') == 'parent')
    child_count = sum(1 for c in chunks if c.get('type') == 'child')
    print(f"   Создано чанков: {len(chunks)} (Parent: {parent_count}, Child: {child_count})")
    
    # Шаг 6: Экспорт для Dify
    print("\n6. Экспорт для Dify...")
    
    # JSON формат (полный)
    json_output_path = output_dir / 'gkrch2023_dify.json'
    with open(json_output_path, 'w', encoding='utf-8') as f:
        f.write(export_for_dify(chunks, 'json'))
    print(f"   JSON сохранен: {json_output_path}")
    
    # CSV формат (для bulk import)
    csv_output_path = output_dir / 'gkrch2023_dify.csv'
    with open(csv_output_path, 'w', encoding='utf-8') as f:
        f.write(export_for_dify(chunks, 'csv'))
    print(f"   CSV сохранен: {csv_output_path}")
    
    # TXT формат (для просмотра)
    txt_output_path = output_dir / 'gkrch2023_chunks.txt'
    with open(txt_output_path, 'w', encoding='utf-8') as f:
        f.write(export_for_dify(chunks, 'txt'))
    print(f"   TXT сохранен: {txt_output_path}")
    
    # Статистика
    print("\n=== Статистика ===")
    total_chars = sum(len(c['content']) for c in chunks)
    avg_parent_chars = sum(len(c['content']) for c in chunks if c.get('type') == 'parent') / max(parent_count, 1)
    avg_child_chars = sum(len(c['content']) for c in chunks if c.get('type') == 'child') / max(child_count, 1)
    
    print(f"Общее количество символов: {total_chars}")
    print(f"Средний размер Parent чанка: {avg_parent_chars:.0f} символов (~{avg_parent_chars//3} токенов)")
    print(f"Средний размер Child чанка: {avg_child_chars:.0f} символов (~{avg_child_chars//3} токенов)")
    
    print("\n=== Готово! ===")
    print(f"\nФайлы для импорта в Dify:")
    print(f"  - {json_output_path} (рекомендуется для API)")
    print(f"  - {csv_output_path} (для Bulk Import через UI)")
    print(f"  - {md_output_path} (Markdown версия документа)")
    
    # Пример первого чанка
    print("\n=== Пример Parent чанка ===")
    for chunk in chunks:
        if chunk.get('type') == 'parent':
            print(f"ID: {chunk['id']}")
            print(f"Content (первые 300 символов): {chunk['content'][:300]}...")
            print(f"Metadata: {chunk['metadata']}")
            print(f"Child IDs: {chunk['child_ids'][:3]}...")
            break
    
    print("\n=== Пример Child чанка ===")
    for chunk in chunks:
        if chunk.get('type') == 'child':
            print(f"ID: {chunk['id']}")
            print(f"Parent ID: {chunk.get('parent_id')}")
            print(f"Content (первые 200 символов): {chunk['content'][:200]}...")
            print(f"Metadata: {chunk['metadata']}")
            break


if __name__ == '__main__':
    main()

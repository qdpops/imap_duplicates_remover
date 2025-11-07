#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IMAP дубликаты писем - многопоточное удаление с интерактивным меню
Поддержка кириллицы и составных имён папок
"""

import imaplib
import email
from email.header import decode_header
from email.message import Message
import hashlib
from collections import defaultdict
import threading
from queue import Queue
import time
from typing import List, Dict, Set, Tuple
import re
import sys
import getpass
import base64

class IMAPDuplicateRemover:
    # Папки, которые нужно пропустить (на разных языках)
    SKIP_FOLDERS = [
        'trash', 'deleted', 'spam', 'junk', 'drafts', 'draft',
        'корзина', 'удаленные', 'спам', 'мусор', 'черновики', 'черновик',
        '[gmail]/trash', '[gmail]/spam', '[gmail]/drafts',
        'deleted items', 'deleted messages', 'junk email'
    ]
    
    def __init__(self, host: str, username: str, password: str, 
                 port: int = 993, use_ssl: bool = True, num_threads: int = 4):
        """
        Инициализация удаления дубликатов IMAP
        
        Args:
            host: IMAP сервер
            username: Имя пользователя
            password: Пароль
            port: Порт (по умолчанию 993 для SSL)
            use_ssl: Использовать SSL
            num_threads: Количество потоков
        """
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_ssl = use_ssl
        self.num_threads = num_threads
        self.lock = threading.Lock()
        self.stats = {
            'total_messages': 0,
            'duplicates_found': 0,
            'duplicates_deleted': 0,
            'errors': 0
        }
    
    def connect(self) -> imaplib.IMAP4_SSL:
        """Создаёт подключение к IMAP серверу"""
        try:
            if self.use_ssl:
                mail = imaplib.IMAP4_SSL(self.host, self.port)
            else:
                mail = imaplib.IMAP4(self.host, self.port)
            
            mail.login(self.username, self.password)
            return mail
        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            raise
    
    def decode_folder_name(self, folder_name: str) -> str:
        """Декодирует имя папки из модифицированного UTF-7 (IMAP)"""
        try:
            if '&' in folder_name:
                decoded = ''
                i = 0
                while i < len(folder_name):
                    if folder_name[i] == '&':
                        end = folder_name.find('-', i)
                        if end == -1:
                            end = len(folder_name)
                        
                        if end == i + 1:
                            decoded += '&'
                            i = end + 1
                        else:
                            encoded_part = folder_name[i+1:end]
                            try:
                                encoded_part = encoded_part.replace(',', '/')
                                padding = (4 - len(encoded_part) % 4) % 4
                                encoded_part += '=' * padding
                                decoded_bytes = base64.b64decode(encoded_part)
                                decoded += decoded_bytes.decode('utf-16-be')
                            except:
                                decoded += folder_name[i:end+1]
                            i = end + 1
                    else:
                        decoded += folder_name[i]
                        i += 1
                return decoded
            else:
                return folder_name
        except Exception as e:
            return folder_name
    
    def should_skip_folder(self, folder_name: str) -> bool:
        """Проверяет, нужно ли пропустить папку"""
        folder_lower = folder_name.lower()
        
        for skip_pattern in self.SKIP_FOLDERS:
            if skip_pattern in folder_lower:
                return True
        
        return False
    
    def get_folders(self, mail: imaplib.IMAP4_SSL, skip_system: bool = True) -> List[str]:
        """Получает список всех папок"""
        folders = []
        try:
            status, folder_list = mail.list()
            if status == 'OK':
                for folder_info in folder_list:
                    try:
                        folder_line = folder_info.decode('ascii', errors='ignore')
                        
                        # DEBUG: показываем что парсим (первые 3 папки)
                        if len(folders) < 3:
                            print(f"  DEBUG RAW: {folder_line}")
                        
                        # Regex для парсинга: (\Flags) "delimiter" "folder_name"
                        pattern1 = r'\([^\)]*\)\s+"([^"]*)"\s+"([^"]*)"'
                        match = re.search(pattern1, folder_line)
                        
                        if match:
                            delimiter = match.group(1)
                            folder_name = match.group(2)
                            if len(folders) < 3:
                                print(f"  DEBUG PARSED: delimiter='{delimiter}', folder='{folder_name}'")
                        else:
                            # Альтернативный формат без кавычек
                            pattern2 = r'\([^\)]*\)\s+"([^"]*)"\s+(\S+)'
                            match = re.search(pattern2, folder_line)
                            if match:
                                delimiter = match.group(1)
                                folder_name = match.group(2).strip()
                            else:
                                continue
                        
                        if not folder_name or folder_name == '.':
                            continue
                        
                        decoded_name = self.decode_folder_name(folder_name)
                        
                        if skip_system and self.should_skip_folder(decoded_name):
                            print(f"  ⏭️  Пропускаю: {decoded_name}")
                            continue
                        
                        folders.append(folder_name)
                        
                    except Exception as e:
                        continue
                        
        except Exception as e:
            print(f"❌ Ошибка получения списка папок: {e}")
        
        return folders
    
    def decode_header_value(self, header_value: str) -> str:
        """Декодирует значение заголовка письма"""
        if not header_value:
            return ""
        
        decoded_parts = []
        try:
            for part, encoding in decode_header(header_value):
                if isinstance(part, bytes):
                    if encoding:
                        try:
                            decoded_parts.append(part.decode(encoding))
                        except:
                            decoded_parts.append(part.decode('utf-8', errors='ignore'))
                    else:
                        decoded_parts.append(part.decode('utf-8', errors='ignore'))
                else:
                    decoded_parts.append(str(part))
        except:
            return str(header_value)
        
        return ''.join(decoded_parts)
    
    def get_message_hash(self, msg: Message) -> str:
        """Создаёт хеш письма"""
        from_header = self.decode_header_value(msg.get('From', ''))
        subject = self.decode_header_value(msg.get('Subject', ''))
        date = msg.get('Date', '')
        message_id = msg.get('Message-ID', '')
        
        unique_str = f"{from_header}|{subject}|{date}|{message_id}"
        return hashlib.md5(unique_str.encode('utf-8')).hexdigest()
    
    def process_folder(self, folder_name: str, dry_run: bool = False) -> Dict:
        """Обрабатывает одну папку и находит дубликаты"""
        mail = None
        folder_stats = {
            'folder': folder_name,
            'total': 0,
            'duplicates': 0,
            'deleted': 0,
            'errors': 0
        }
        
        try:
            mail = self.connect()
            display_name = self.decode_folder_name(folder_name)
            
            # Выбираем папку используя оригинальное имя В КАВЫЧКАХ
            status = 'NO'
            
            try:
                # IMAP требует имя папки в кавычках если есть пробелы или спецсимволы
                status, messages = mail.select('"{}"'.format(folder_name), readonly=False)
            except Exception as e:
                # Если не получилось с кавычками, пробуем без
                try:
                    status, messages = mail.select(folder_name, readonly=False)
                except:
                    pass
            
            if status != 'OK':
                print(f"❌ Не удалось открыть папку: {display_name}")
                print(f"   DEBUG: Имя для IMAP: {folder_name}")
                return folder_stats
            
            status, msg_nums = mail.search(None, 'ALL')
            if status != 'OK':
                return folder_stats
            
            message_ids = msg_nums[0].split()
            folder_stats['total'] = len(message_ids)
            
            print(f"\n📁 Папка: {display_name}")
            print(f"   Всего писем: {len(message_ids)}")
            
            if len(message_ids) == 0:
                print(f"   ℹ️  Папка пустая, пропускаем")
                return folder_stats
            
            hash_to_ids = defaultdict(list)
            
            processed = 0
            for msg_id in message_ids:
                try:
                    status, msg_data = mail.fetch(msg_id, '(RFC822)')
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    msg_hash = self.get_message_hash(msg)
                    hash_to_ids[msg_hash].append(msg_id)
                    
                    processed += 1
                    if processed % 50 == 0:
                        print(f"   📊 Обработано: {processed}/{len(message_ids)}", end='\r')
                    
                except Exception as e:
                    folder_stats['errors'] += 1
            
            if processed > 0:
                print(f"   📊 Обработано: {processed}/{len(message_ids)}")
            
            duplicates_count = 0
            deleted_count = 0
            
            for msg_hash, ids in hash_to_ids.items():
                if len(ids) > 1:
                    duplicates_count += len(ids) - 1
                    
                    for duplicate_id in ids[1:]:
                        if not dry_run:
                            try:
                                mail.store(duplicate_id, '+FLAGS', '\\Deleted')
                                deleted_count += 1
                            except Exception as e:
                                folder_stats['errors'] += 1
            
            if not dry_run and deleted_count > 0:
                mail.expunge()
            
            folder_stats['duplicates'] = duplicates_count
            folder_stats['deleted'] = deleted_count
            
            if duplicates_count > 0:
                print(f"   ✅ Найдено дубликатов: {duplicates_count}")
                if not dry_run:
                    print(f"   🗑️  Удалено: {deleted_count}")
            else:
                print(f"   ✨ Дубликатов не найдено")
            
        except Exception as e:
            display_name = self.decode_folder_name(folder_name)
            print(f"❌ Ошибка обработки папки {display_name}: {e}")
            folder_stats['errors'] += 1
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except:
                    pass
        
        return folder_stats
    
    def worker(self, queue: Queue, results: List, dry_run: bool):
        """Рабочий поток для обработки папок"""
        while True:
            folder = queue.get()
            if folder is None:
                break
            
            result = self.process_folder(folder, dry_run)
            
            with self.lock:
                results.append(result)
                self.stats['total_messages'] += result['total']
                self.stats['duplicates_found'] += result['duplicates']
                self.stats['duplicates_deleted'] += result['deleted']
                self.stats['errors'] += result['errors']
            
            queue.task_done()
    
    def remove_duplicates(self, folders: List[str] = None, dry_run: bool = False, skip_system: bool = True):
        """Удаляет дубликаты из указанных папок"""
        mode_text = "ПРОВЕРКА" if dry_run else "УДАЛЕНИЕ"
        
        print("\n" + "=" * 70)
        print(f"🔍 IMAP Поиск дубликатов писем - Режим: {mode_text}")
        print("=" * 70)
        
        print("\n📂 Подключение к серверу...")
        mail = self.connect()
        print("✅ Подключено успешно!")
        
        if folders is None:
            print(f"\n📋 Получение списка папок...")
            folders = self.get_folders(mail, skip_system=skip_system)
        
        mail.logout()
        
        if not folders:
            print("\n⚠️  Не найдено папок для обработки!")
            return
        
        print(f"\n📁 Найдено папок для обработки: {len(folders)}")
        for i, folder in enumerate(folders, 1):
            display_name = self.decode_folder_name(folder)
            print(f"   {i}. {display_name}")
        
        queue = Queue()
        results = []
        threads = []
        
        print(f"\n🚀 Запуск обработки ({self.num_threads} потоков)...")
        
        for i in range(min(self.num_threads, len(folders))):
            t = threading.Thread(target=self.worker, args=(queue, results, dry_run))
            t.start()
            threads.append(t)
        
        for folder in folders:
            queue.put(folder)
        
        queue.join()
        
        for i in range(len(threads)):
            queue.put(None)
        for t in threads:
            t.join()
        
        print("\n" + "=" * 70)
        print("📊 ИТОГОВАЯ СТАТИСТИКА")
        print("=" * 70)
        print(f"📧 Всего писем обработано: {self.stats['total_messages']}")
        print(f"🔍 Найдено дубликатов: {self.stats['duplicates_found']}")
        if not dry_run:
            print(f"🗑️  Удалено дубликатов: {self.stats['duplicates_deleted']}")
        if self.stats['errors'] > 0:
            print(f"⚠️  Ошибок: {self.stats['errors']}")
        print("=" * 70)


def print_menu():
    """Выводит главное меню"""
    print("\n" + "=" * 70)
    print("📧 IMAP УДАЛЕНИЕ ДУБЛИКАТОВ ПИСЕМ")
    print("=" * 70)
    print("\n1. 🔍 Только проверка (без удаления)")
    print("2. 🗑️  Удалить дубликаты")
    print("3. ⚙️  Изменить настройки")
    print("4. ❌ Выход")
    print("\n" + "=" * 70)


def get_imap_settings():
    """Интерактивный ввод настроек IMAP"""
    print("\n" + "=" * 70)
    print("⚙️  НАСТРОЙКИ ПОДКЛЮЧЕНИЯ")
    print("=" * 70)
    
    print("\n📌 Популярные серверы:")
    print("   Gmail:     imap.gmail.com")
    print("   Yandex:    imap.yandex.ru")
    print("   Mail.ru:   imap.mail.ru")
    print("   Outlook:   outlook.office365.com")
    print("   Timeweb:   imap.timeweb.ru или mail.timeweb.ru")
    print("   Beget:     imap.beget.com")
    
    host = input("\n🌐 IMAP сервер: ").strip()
    
    port_input = input("🔌 Порт [993]: ").strip()
    port = int(port_input) if port_input else 993
    
    username = input("👤 Email/Логин: ").strip()
    password = getpass.getpass("🔑 Пароль: ")
    
    threads_input = input("⚡ Количество потоков [4]: ").strip()
    threads = int(threads_input) if threads_input else 4
    
    return {
        'host': host,
        'port': port,
        'username': username,
        'password': password,
        'num_threads': threads
    }


def main():
    """Главная функция с интерактивным меню"""
    settings = None
    
    print("\n" + "🌟" * 35)
    print("Добро пожаловать в IMAP Duplicate Remover!")
    print("🌟" * 35)
    
    while True:
        if settings is None:
            settings = get_imap_settings()
        
        print_menu()
        
        choice = input("Выберите действие [1-4]: ").strip()
        
        if choice == '1':
            print("\n🔍 Запуск проверки на дубликаты...")
            try:
                remover = IMAPDuplicateRemover(
                    host=settings['host'],
                    username=settings['username'],
                    password=settings['password'],
                    port=settings['port'],
                    num_threads=settings['num_threads']
                )
                remover.remove_duplicates(dry_run=True, skip_system=True)
                
                print("\n✅ Проверка завершена!")
                
            except Exception as e:
                print(f"\n❌ Ошибка: {e}")
                print("💡 Проверьте настройки подключения")
        
        elif choice == '2':
            print("\n⚠️  ВНИМАНИЕ! Будут удалены найденные дубликаты!")
            confirm = input("Продолжить? (yes/no): ").strip().lower()
            
            if confirm in ['yes', 'y', 'да', 'д']:
                print("\n🗑️  Запуск удаления дубликатов...")
                try:
                    remover = IMAPDuplicateRemover(
                        host=settings['host'],
                        username=settings['username'],
                        password=settings['password'],
                        port=settings['port'],
                        num_threads=settings['num_threads']
                    )
                    remover.remove_duplicates(dry_run=False, skip_system=True)
                    
                    print("\n✅ Удаление завершено!")
                    
                except Exception as e:
                    print(f"\n❌ Ошибка: {e}")
                    print("💡 Проверьте настройки подключения")
            else:
                print("\n❌ Операция отменена")
        
        elif choice == '3':
            settings = get_imap_settings()
            print("\n✅ Настройки обновлены!")
        
        elif choice == '4':
            print("\n👋 До свидания!")
            sys.exit(0)
        
        else:
            print("\n❌ Неверный выбор. Попробуйте снова.")
        
        input("\nНажмите Enter для продолжения...")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Программа прервана пользователем. До свидания!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        sys.exit(1)

import os, json, uuid, shutil, logging, subprocess, sqlite3, re, random, tempfile
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger('history_bot.youtube_echo')


class HistoryEchoPublisher:
    """High-quality vertical Shorts pipeline.

    Design priorities: natural speech first, audible but unobtrusive music,
    and clean 1080x1920 rendering from a single generated image.
    """

    def __init__(self, db_file, work_dir='runtime/youtube'):
        self.db_file = db_file
        # Every publication gets an isolated workspace.
        # This prevents manual and scheduled runs from deleting each other's files.
        self.work_root = Path(work_dir)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.work = self.work_root / f"run-{uuid.uuid4().hex}"
        self.work.mkdir(parents=True, exist_ok=True)
        self.enabled = os.getenv('HISTORY_ECHO_YOUTUBE_ENABLED', '0').strip() == '1'
        self.title_prefix = os.getenv('HISTORY_ECHO_TITLE_PREFIX', 'История, о которой стоит знать').strip()
        self.privacy = os.getenv('YOUTUBE_PRIVACY_STATUS', 'public').strip()
        self.client_secret = os.getenv('HISTORY_ECHO_YOUTUBE_CLIENT_SECRET_FILE', 'youtube_client_secret.json').strip()
        self.token_file = os.getenv('HISTORY_ECHO_YOUTUBE_TOKEN_FILE', 'history_echo_youtube_token.json').strip()
        self.music_enabled = os.getenv('HISTORY_ECHO_MUSIC_ENABLED', '1').strip() != '0'

        # A slower delivery sounds substantially less robotic for reflective text.
        self.voice = os.getenv('HISTORY_ECHO_TTS_VOICE', 'ru-RU-DmitryNeural').strip()
        self.rate = os.getenv('HISTORY_ECHO_TTS_RATE', '-8%').strip()
        self.pitch = os.getenv('HISTORY_ECHO_TTS_PITCH', '-2Hz').strip()
        self.volume = os.getenv('EDGE_TTS_VOLUME', '+0%').strip()
        self.duration = int(os.getenv('HISTORY_ECHO_MAX_SECONDS', '58'))
        # Static image is intentional: reliable fast rendering on a modest VPS.
        self.ken_burns = False
        self.music_level = float(os.getenv('HISTORY_ECHO_MUSIC_LEVEL', '0.12'))
        self.music_library = Path(os.getenv('HISTORY_ECHO_MUSIC_LIBRARY', '/opt/history-bot/persistent/music'))
        self.keep_failed_artifacts = os.getenv('YOUTUBE_KEEP_FAILED_ARTIFACTS', '0').strip() == '1'

    def ensure_db(self):
        with sqlite3.connect(self.db_file) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS youtube_publications (
              id INTEGER PRIMARY KEY AUTOINCREMENT, quote_hash TEXT, video_id TEXT,
              title TEXT, status TEXT NOT NULL, published_at TEXT, error TEXT,
              UNIQUE(quote_hash))''')
            c.commit()

    def _run(self, args, timeout=240):
        args = list(args)
        if args and Path(args[0]).name == 'ffmpeg' and '-nostdin' not in args:
            args.insert(1, '-nostdin')
        try:
            p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Command timeout after {timeout}s: {' '.join(map(str, args[:8]))}") from exc
        if p.returncode != 0:
            output = (p.stderr or p.stdout or '').strip()
            raise RuntimeError(output[-4000:])

    def _clean(self):
        """Remove only ephemeral render artifacts; never touch the music library."""
        for name in {'image.jpg', 'voice.mp3', 'music.mp3', 'short.mp4',
                     '_voice_concat.txt', 'voice_segment_*.mp3'}:
            if '*' in name:
                candidates = self.work.glob(name)
            else:
                candidates = [self.work / name]
            for p in candidates:
                try:
                    if p.exists() and (p.is_file() or p.is_symlink()):
                        p.unlink()
                except Exception as exc:
                    log.warning('cleanup %s failed: %s', p, exc)

    def _pick_user_music(self):
        """Choose a persistent user-provided track without copying the library."""
        if not self.music_library.is_dir():
            return None
        allowed = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac'}
        tracks = [p for p in self.music_library.iterdir()
                  if p.is_file() and p.suffix.lower() in allowed and not p.name.startswith('.')]
        return random.choice(tracks) if tracks else None

    def make_script(self, article, topic, llm=None):
        """Create a complete, fact-faithful mini-article for a <=58s Short.
        The script must contain the whole selected story, not a teaser or summary.
        """
        if llm:
            prompt = (
                'Ты редактор исторического YouTube-канала History Echo. '
                'Подготовь ОДИН полностью законченный текст для озвучки Shorts. '
                'Текст должен быть полностью законченным и помещаться в допустимую длительность Shorts; не обрезай и не выбрасывай смысловые части. '
                'Нельзя обрывать статью, пропускать ключевую развязку или оставлять обещание продолжения. '
                'Структура: 1) цепляющее начало в первой фразе; 2) конкретные проверяемые факты; '
                '3) короткая неожиданная деталь или смысловой поворот; 4) естественная финальная фраза, '
                'текст должен быть только содержанием истории, без призывов подписаться, названия канала и хештегов. Не выдумывай факты. '
                'Без заголовка, списков, хэштегов, эмодзи и служебных комментариев. '
                'Для русской озвучки расставляй естественные знаки препинания: запятые для коротких пауз, '
                'тире для смыслового акцента, двоеточие перед пояснением. Числа пиши словами. '
                'Проверяй ударения в именах, географических названиях и неоднозначных словах; используй знак ударения только там, где он помогает диктору. '
                'Не добавляй SSML-теги и не используй сокращения. '
                f'Тема: {topic}\nИсходная статья/факты: {article}'
            )
            text = str(llm(prompt) or '').strip()
            words = len(re.findall(r'\S+', text))
            if 60 <= words <= 115 and len(text) <= 1100 and text[-1:] in '.!?…':
                return text
            log.warning('History Echo script rejected words=%s chars=%s', words, len(text))
        return self._fallback_script(article, topic)

    def _fallback_script(self, article, topic):
        """Sentence-complete fallback; never cut in the middle of a sentence."""
        text = re.sub(r'\s+', ' ', str(article or '')).strip()
        sentences = re.split(r'(?<=[.!?…])\s+', text)
        chosen, words = [], 0
        for sentence in sentences:
            count = len(sentence.split())
            if chosen and words + count > 92:
                break
            chosen.append(sentence.strip())
            words += count
            if words >= 75:
                break
        # Never truncate source facts: retain the complete article in fallback mode.
        body = text
        body = re.sub(r'(?<![.!?…])\s+(—|–)\s+', r' — ', body)
        return (f'Знали ли вы эту историю? {body} '
                '')

    def _voice_segments(self, text):
        """Split at sentence boundaries so neural TTS gets natural breathing pauses."""
        text = re.sub(r'\s+', ' ', text).strip()
        parts = re.split(r'(?<=[.!?…])\s+', text)
        result = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) > 260:
                chunks = re.split(r'(?<=[,;:—])\s+', part)
                result.extend(x.strip() for x in chunks if x.strip())
            else:
                result.append(part)
        return result or [text]

    def _prepare_tts_text(self, text):
        """Prepare Russian text for natural neural TTS delivery."""
        text = str(text or '')
        text = re.sub(r'\s+', ' ', text).strip()

        # Remove emoji and pictographic symbols before TTS.
        text = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF]', '', text)

        # Normalize Russian year endings for natural pronunciation.
        text = re.sub(r'\b(1[0-9]{3})-(?:м|му)\b', r'\1 году', text)

        # Remove square brackets while preserving their content.
        text = re.sub(r'\[([^]]+)\]', r'\1', text)

        # Normalize repeated punctuation and spacing.
        text = re.sub(r'([.!?])\1+', r'\1', text)
        text = re.sub(r':(?=\S)', ': ', text)

        return re.sub(r'\s+', ' ', text).strip()

    def synthesize(self, text):
        """Synthesize the complete Russian script in one Edge TTS request."""
        text = str(text or '')

        # Do not speak hashtags or promotional phrases.
        text = re.sub(r'#[\\wа-яА-ЯёЁ-]+', '', text)
        text = re.sub(
            r'(?i)(подписывайтесь|подпишитесь|подписка)[^.!?]*[.!?]?',
            '',
            text
        )

        text = re.sub(r'["]', '', text)
        text = self._prepare_tts_text(text)

        import asyncio
        import edge_tts

        out = self.work / 'voice.mp3'
        last_error = None

        async def go():
            nonlocal last_error

            for attempt in range(1, 4):
                out.unlink(missing_ok=True)

                try:
                    log.info(
                        'Edge TTS request attempt=%s chars=%s voice=%s rate=%s pitch=%s',
                        attempt, len(text), self.voice, self.rate, self.pitch
                    )

                    communicate = edge_tts.Communicate(
                        text,
                        self.voice,
                        rate=self.rate,
                        pitch=self.pitch,
                        volume=self.volume,
                    )

                    await communicate.save(str(out))

                    if out.exists() and out.stat().st_size >= 1000:
                        log.info(
                            'Edge TTS success attempt=%s size=%s',
                            attempt, out.stat().st_size
                        )
                        return

                    last_error = RuntimeError(
                        f'Edge TTS returned empty audio: '
                        f'{out.stat().st_size if out.exists() else 0} bytes'
                    )

                except Exception as exc:
                    last_error = exc
                    log.warning(
                        'Edge TTS attempt failed attempt=%s error=%r',
                        attempt, exc
                    )

                if attempt < 3:
                    await asyncio.sleep(2)

            raise RuntimeError(
                f'Edge TTS failed after 3 attempts: {last_error}'
            )

        asyncio.run(go())

        if not out.exists() or out.stat().st_size < 1000:
            raise RuntimeError('Edge TTS produced empty audio')

        return out

    def get_audio_duration(self, audio_file):
        probe = subprocess.run([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(audio_file)
        ], capture_output=True, text=True, check=True)
        try:
            duration = float(probe.stdout.strip())
        except Exception as exc:
            raise RuntimeError(f'Cannot determine audio duration: {probe.stdout}') from exc
        return max(duration, 1.0)

    def ambient_music(self, duration=58):
        """Warm, audible ambient bed with movement; no external copyrighted source."""
        out = self.work / 'music.mp3'
        out.unlink(missing_ok=True)
        dur = min(max(float(duration), 1.0), float(self.duration))
        fade_out_start = max(0.5, dur - 2.5)
        # Three detuned tones + soft filtered noise. This is deliberately richer than two quiet sines.
        fc = (
            '[0:a]volume=0.055,lowpass=f=900[a0];'
            '[1:a]volume=0.040,lowpass=f=1200[a1];'
            '[2:a]volume=0.030,lowpass=f=1500[a2];'
            '[3:a]volume=0.012,lowpass=f=500,highpass=f=120[n];'
            '[a0][a1][a2][n]amix=inputs=4:normalize=0,'
            'aecho=0.8:0.5:80:0.20,'
            f'afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_start:.3f}:d=2.5,'
            'alimiter=limit=0.85[m]'
        )
        self._run([
            'ffmpeg', '-y',
            '-f', 'lavfi', '-i', 'sine=frequency=174.61:sample_rate=44100',
            '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=44100',
            '-f', 'lavfi', '-i', 'sine=frequency=261.63:sample_rate=44100',
            '-f', 'lavfi', '-i', 'anoisesrc=color=pink:sample_rate=44100',
            '-filter_complex', fc, '-map', '[m]', '-t', str(dur),
            '-c:a', 'libmp3lame', '-q:a', '2', str(out)
        ])
        if not out.exists() or out.stat().st_size < 1000:
            raise RuntimeError('Ambient music was not created')
        return out

    def render(self, image, voice, music=None):
        """Fast production render: static image, voice first, clearly audible music."""
        img = self.work / 'image.jpg'
        img.write_bytes(image)
        out = self.work / 'short.mp4'
        out.unlink(missing_ok=True)
        duration = self.get_audio_duration(voice)
        
        inputs = [
            'ffmpeg', '-nostdin', '-y', '-loop', '1', '-framerate', '12', '-i', str(img),
            '-i', str(voice)
        ]
        has_music = bool(music and music.exists())
        if has_music:
            inputs += ['-stream_loop', '-1', '-i', str(music)]

        # Static visual policy: the generated artwork must remain completely still.
        # A video container is still required by YouTube, but there is no zoom, drift,
        # light breathing, transition, overlay, or other animation.
        video_filter = (
            '[0:v]'
            'scale=1080:1920:force_original_aspect_ratio=increase,'
            'crop=1080:1920,'
            'setsar=1,format=yuv420p[v]'
        )
        # Add a safety tail: MP3 duration can be a little shorter than decoded audio.
        # Without it, -t may cut the final word or consonant.
        render_duration = duration + 0.90
        if has_music:
            level = max(0.18, min(self.music_level, 0.55))
            audio_filter = (
                '[1:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=7,apad=pad_dur=1.0,asplit=2[voice_main][voice_side];'
                f'[2:a]aresample=48000,volume={level:.3f},afade=t=in:st=0:d=1.2,afade=t=out:st={max(0.5, render_duration-2.0):.3f}:d=2.0[music];'
                '[music][voice_side]sidechaincompress=threshold=0.035:ratio=8:attack=25:release=350:makeup=1[ducked];'
                '[voice_main][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.94[a]'
            )
        else:
            audio_filter = (
                '[1:a]aresample=48000,loudnorm=I=-15:TP=-1.5:LRA=7,apad=pad_dur=1.0,'
                'alimiter=limit=0.96[a]'
            )

        args = inputs + [
            '-filter_complex', video_filter + ';' + audio_filter,
            '-map', '[v]', '-map', '[a]', '-t', f'{render_duration:.3f}', '-r', '24',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-profile:v', 'high', '-level', '4.1', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(out)
        ]
        self._run(args, timeout=600)
        if not out.exists() or out.stat().st_size < 10000:
            raise RuntimeError('FFmpeg did not create valid video')

        verify = subprocess.run([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_name,width,height',
            '-of', 'json', str(out)
        ], capture_output=True, text=True)
        if verify.returncode != 0:
            raise RuntimeError('FFmpeg produced invalid MP4: ' + (verify.stderr or verify.stdout))
        try:
            info = json.loads(verify.stdout)
            duration_out = float(info['format']['duration'])
            streams = info.get('streams', [])
            has_h264 = any(s.get('codec_name') == 'h264' for s in streams)
            has_aac = any(s.get('codec_name') == 'aac' for s in streams)
            has_vertical = any(s.get('width') == 1080 and s.get('height') == 1920 for s in streams)
        except Exception as exc:
            raise RuntimeError('Cannot parse rendered video metadata') from exc
        if not (has_h264 and has_aac and has_vertical and 1 <= duration_out <= 180):
            raise RuntimeError('Rendered video failed production validation')
        log.info('YouTube render validated duration=%.3f size=%s music=%s static_image=True music_level=%.3f safety_tail=True',
                 duration_out, out.stat().st_size, has_music, self.music_level)
        return out

    def upload(self, video, title, description, tags):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError as e:
            raise RuntimeError('YouTube dependencies not installed') from e
        scopes = ['https://www.googleapis.com/auth/youtube.upload']
        creds = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, scopes)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds:
            if not Path(self.client_secret).exists():
                raise RuntimeError(f'YouTube OAuth file not found: {self.client_secret}')
            flow = InstalledAppFlow.from_client_secrets_file(self.client_secret, scopes)
            creds = flow.run_console()
        Path(self.token_file).write_text(creds.to_json(), encoding='utf-8')
        yt = build('youtube', 'v3', credentials=creds)
        body = {'snippet': {'title': title[:100], 'description': description[:5000], 'tags': tags[:15], 'categoryId': '22'},
                'status': {'privacyStatus': self.privacy, 'selfDeclaredMadeForKids': False}}
        req = yt.videos().insert(part='snippet,status', body=body,
                                 media_body=MediaFileUpload(str(video), resumable=True, mimetype='video/mp4'))
        response = None
        last_error = None
        # Retry the SAME resumable request. Creating a new insert request after
        # an uncertain network failure can create duplicate YouTube videos.
        for attempt in range(1, 13):
            try:
                while response is None:
                    _, response = req.next_chunk()
                break
            except Exception as exc:
                last_error = exc
                log.warning('YouTube upload chunk failed %s/12: %s', attempt, exc)
                if attempt >= 12:
                    raise RuntimeError(
                        'YouTube upload failed after resumable retries; upload state is uncertain'
                    ) from exc
                import time
                time.sleep(min(30 * attempt, 180))
        if not response or not response.get('id'):
            raise RuntimeError(f'YouTube upload returned no video id: {last_error}')
        return response['id']

    def publish(self, content, image_bytes, quote_hash, llm=None):
        self.ensure_db()
        if not self.enabled:
            log.info('YouTube disabled')
            return False, None, 'disabled'
        log.info('YouTube publication started quote_hash=%s', quote_hash)
        with sqlite3.connect(self.db_file) as c:
            row = c.execute('SELECT video_id,status FROM youtube_publications WHERE quote_hash=?', (quote_hash,)).fetchone()
            if row and row[0]:
                log.info('YouTube video already published video_id=%s', row[0])
                return True, row[0], None
        success = False
        try:
            log.info('YouTube step 1/5: generating script')
            quote = content.get('translated_quote') or content['quote']['quote_text']
            author = content.get('translated_author') or content['quote']['author']
            script = self.make_script(quote, author, llm)
            log.info('YouTube script generated chars=%s', len(script))
            log.info('YouTube step 2/5: synthesizing voice voice=%s rate=%s pitch=%s', self.voice, self.rate, self.pitch)
            voice = self.synthesize(script)
            log.info('YouTube voice created size=%s', voice.stat().st_size)
            music = None
            if self.music_enabled:
                music = self._pick_user_music()
                if music:
                    log.info('YouTube step 3/5: using persistent user music %s', music.name)
                else:
                    log.info('YouTube step 3/5: generating lightweight ambient fallback')
                    voice_duration = self.get_audio_duration(voice)
                    music = self.ambient_music(voice_duration)
                    log.info('YouTube ambient music created size=%s', music.stat().st_size)
            log.info('YouTube step 4/5: rendering video')
            video = self.render(image_bytes, voice, music)
            log.info('YouTube video rendered size=%s', video.stat().st_size)
            title = f'{self.title_prefix}: {quote}'[:100]
            description = f'{quote}\n— {author}\n\n#цитата #мысли #shorts'
            log.info('YouTube step 5/5: uploading video')
            video_id = self.upload(video, title, description, ['цитата', 'мысли', 'shorts', 'мотивация'])
            with sqlite3.connect(self.db_file) as c:
                c.execute('''INSERT OR REPLACE INTO youtube_publications
                    (quote_hash, video_id, title, status, published_at, error)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (quote_hash, video_id, title, 'success', datetime.now(timezone.utc).isoformat(), ''))
                c.commit()
            success = True
            log.info('YouTube publication SUCCESS video_id=%s', video_id)
            return True, video_id, None
        except Exception as exc:
            log.exception('YouTube pipeline failed')
            error_text = str(exc)
            try:
                with sqlite3.connect(self.db_file) as c:
                    c.execute('''INSERT OR REPLACE INTO youtube_publications
                        (quote_hash, video_id, title, status, published_at, error)
                        VALUES (?, ?, ?, ?, ?, ?)''',
                        (quote_hash, '', '', 'error', datetime.now(timezone.utc).isoformat(), error_text[:2000]))
                    c.commit()
            except Exception:
                log.exception('YouTube error state save failed')
            return False, None, error_text
        finally:
            if success or not self.keep_failed_artifacts:
                self._clean()
            else:
                log.warning('YouTube failed; temporary files preserved for diagnostics in %s', self.work)

    def publish_article(self, article, image_bytes, article_key, topic, llm=None):
        """Prepare and publish one complete daily historical mini-article."""
        self.ensure_db()
        if not self.enabled:
            return False, None, 'disabled'
        with sqlite3.connect(self.db_file) as c:
            row = c.execute('SELECT video_id,status FROM youtube_publications WHERE quote_hash=?', (article_key,)).fetchone()
            if row and row[0]:
                return True, row[0], None
        success = False
        try:
            # Используем готовую статью из единого материала Telegram/MAX.
            # Повторная генерация через GigaChat не нужна.
            script = article
            voice = self.synthesize(script)
            music = self._pick_user_music() if self.music_enabled else None
            if self.music_enabled and not music:
                music = self.ambient_music(self.get_audio_duration(voice))
            video = self.render(image_bytes, voice, music)
            title = f'{self.title_prefix}: {topic}'[:100]
            description = f'{topic}\n\n{script}\n\n#история #history #shorts'
            video_id = self.upload(video, title, description, ['история', 'history', 'исторические факты', 'shorts'])
            with sqlite3.connect(self.db_file) as c:
                c.execute('INSERT OR REPLACE INTO youtube_publications (quote_hash, video_id, title, status, published_at, error) VALUES (?, ?, ?, ?, ?, ?)', (article_key, video_id, title, 'success', datetime.now(timezone.utc).isoformat(), ''))
                c.commit()
            success = True
            return True, video_id, None
        except Exception as exc:
            with sqlite3.connect(self.db_file) as c:
                c.execute('INSERT OR REPLACE INTO youtube_publications (quote_hash, video_id, title, status, published_at, error) VALUES (?, ?, ?, ?, ?, ?)', (article_key, '', '', 'error', datetime.now(timezone.utc).isoformat(), str(exc)[:2000]))
                c.commit()
            return False, None, str(exc)
        finally:
            if success or not self.keep_failed_artifacts:
                self._clean()

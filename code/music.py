import discord
from discord.ext import commands
import random
import asyncio
import itertools
import sys
import traceback
from async_timeout import timeout
from functools import partial
import yt_dlp as youtube_dl
from yt_dlp import YoutubeDL

# Подавление шума об использовании консоли из-за ошибок
youtube_dl.utils.bug_reports_message = lambda: ''

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # адреса ipv6 иногда вызывают проблемы
}

ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Пользовательский класс исключений для ошибок подключения."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Исключение составляют случаи недействительных голосовых каналов."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')
        self.duration = data.get('duration')

        # YTDL документация
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Позволяет нам получить доступ к атрибутам, аналогичным dict.
        Это полезно только тогда, когда вы НЕ загружаете.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # выберите первый элемент из списка воспроизведения
            data = data['entries'][0]

        embed = discord.Embed(title="",
                              description=f"\n__В очереди__:\n [{data['title']}]({data['webpage_url']}) [{ctx.author.mention}]",
                              color=discord.Color.green())
        await ctx.send(embed=embed, delete_after=300)

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source, "D:\\tofu-delivery\\code\\ffmpeg.exe"), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Используется для подготовки потока вместо загрузки.
        Поскольку срок действия ссылок на потоковое вещание Youtube истекает."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url'], executable="D:\\tofu-delivery\\code\\ffmpeg.exe"), data=data, requester=requester)


class MusicPlayer:
    """Класс, который присваивается каждой гильдии с помощью бота для музыки.
    Этот класс реализует очередь и цикл, которые позволяют разным гильдиям одновременно прослушивать разные плейлисты.
    Когда бот отключится от Голоса, его экземпляр будет уничтожен.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Теперь воспроизводится сообщение
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Наш основной цикл игроков."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Ждите следующей песни. Если мы пропустим тайм-аут, отмените воспроизведение и отключитесь...
                async with timeout(300):  # 5 минут...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Источником, вероятно, был поток (не загруженный)
                # Поэтому мы должны собраться, чтобы предотвратить истечение потока
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'Произошла ошибка при обработке вашей песни.\n'
                                             f'```css\n[{e}]\n```', delete_after=300)
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            embed = discord.Embed(title="Сейчас играет",
                                  description=f"[{source.title}]({source.web_url}) [{source.requester.mention}]",
                                  color=discord.Color.green())
            self.np = await self._channel.send(embed=embed, delete_after=300)
            await self.next.wait()

            # Убедиться, что процесс FFmpeg очищен.
            source.cleanup()
            self.current = None

    def destroy(self, guild):
        """Отключение и очистка проигрывателя."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Команды, связанные с музыкой."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """Локальная проверка, которая применяется ко всем командам в этом винтике."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """Локальный обработчик ошибок для всех ошибок, возникающих из-за команд в этом шестеренчатом механизме."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('Эта команда не может быть использована в личных сообщениях.', delete_after=300)
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Ошибка подключения к голосовому каналу.'
                           'Пожалуйста, убедитесь, что вы находитесь на действительном канале или предоставьте мне один из них.',
                           delete_after=300)

        print('Игнорирование исключения в команде {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Найдите игрока гильдии или сгенерируйте его."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='join', aliases=['connect', 'j'], description="Подключение к голосовому каналу.")
    async def connect_(self, ctx, *, channel: discord.VoiceChannel = None):
        """Подключение к голосовому каналу.
        Параметры
        ------------
        канал: discord.VoiceChannel [Optional]
            Канал, к которому нужно подключиться. Если канал не указан, будет предпринята попытка подключиться к голосовому каналу, на котором вы находитесь.
        Эта команда также обрабатывает перемещение бота по разным каналам.
        """
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                embed = discord.Embed(title="",
                                      description="Нет канала для присоединения. Пожалуйста, введите '$join' , находясь в голосовом канале.",
                                      color=discord.Color.green())
                await ctx.send(embed=embed, delete_after=300)
                raise InvalidVoiceChannel(
                    'Нет канала для присоединения. Пожалуйста, либо укажите действительный канал, либо присоединяйтесь к нему.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Переход к каналу: <{channel}> таймаут.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Подключение к каналу: <{channel}> таймаут.')
        if (random.randint(0, 1) == 0):
            await ctx.message.add_reaction('👍')
        await ctx.send(f'**Подключён `{channel}`**', delete_after=300)

    @commands.command(name='play', aliases=['sing', 'p'], description="Играть музыку.")
    async def play_(self, ctx, *, search: str):
        """Запросите песню и добавьте ее в очередь.
        Эта команда пытается подключиться к действительному голосовому каналу, если бот еще не находится в нем.
        Использует YTDL для автоматического поиска и извлечения песни.
        Параметры
        ------------
        поиск: str [Required]
            Песня для поиска и извлечения с помощью YTDL. Это может быть простой поиск, идентификатор или URL-адрес.
        """

        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        # Если загрузка имеет значение False, источником будет dict, который позже будет использоваться для сбора потока.
        # Если значение загрузки равно True, источником будет discord.FFmpeg PCM Audio с преобразователем громкости.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

        await player.queue.put(source)

    @commands.command(name='pause', description="Поставить музыку на паузу.")
    async def pause_(self, ctx):
        """Поставить на трек на паузу."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            embed = discord.Embed(title="", description="В настоящее время ничего не играет.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send("Пауза ⏸️", delete_after=300)

    @commands.command(name='resume', description="Возобновить музыку.")
    async def resume_(self, ctx):
        """Возобновить трек."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send("Возабновлено ⏯️", delete_after=300)

    @commands.command(name='skip', description="Пропустить следующую песню в очереди.")
    async def skip_(self, ctx):
        """Пропустить песню."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()

    @commands.command(name='remove', aliases=['rm', 'rem'], description="Удаляет указанную песню из очереди")
    async def remove_(self, ctx, pos: int = None):
        """Удаляет указанную песню из очереди."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        player = self.get_player(ctx)
        if pos == None:
            player.queue._queue.pop()
        else:
            try:
                s = player.queue._queue[pos - 1]
                del player.queue._queue[pos - 1]
                embed = discord.Embed(title="",
                                      description=f"Удалён [{s['title']}]({s['webpage_url']}) [{s['requester'].mention}]",
                                      color=discord.Color.green())
                await ctx.send(embed=embed, delete_after=300)
            except:
                embed = discord.Embed(title="", description=f'Не могу найти трек для "{pos}"',
                                      color=discord.Color.green())
                await ctx.send(embed=embed, delete_after=300)

    @commands.command(name='clear', aliases=['clr', 'cl', 'cr'], description="Очищает всю очередь")
    async def clear_(self, ctx):
        """Удаляет всю очередь предстоящих песен."""

        await ctx.channel.purge(limit=2)

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        player = self.get_player(ctx)
        player.queue._queue.clear()
        await ctx.send('💣 **Очищено**', delete_after=300)

    @commands.command(name='queue', aliases=['q', 'playlist', 'que'], description="Показывает очередь")
    async def queue_info(self, ctx):
        """Извлечь основную очередь предстоящих песен."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        player = self.get_player(ctx)
        if player.queue.empty():
            embed = discord.Embed(title="", description="Очередь пуста.", color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        seconds = vc.source.duration % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        if hour > 0:
            duration = "%dh %02dm %02ds" % (hour, minutes, seconds)
        else:
            duration = "%02dm %02ds" % (minutes, seconds)

        # Захватывает песни в очереди...
        upcoming = list(itertools.islice(player.queue._queue, 0, int(len(player.queue._queue))))
        fmt = '\n'.join(
            f"`{(upcoming.index(_)) + 1}.` [{_['title']}]({_['webpage_url']}) | ` {duration} Запрошено: {_['requester']}`\n"
            for _ in upcoming)
        fmt = f"\n__Сейчас играет__:\n[{vc.source.title}]({vc.source.web_url}) | ` {duration} Запрошено: {vc.source.requester}`\n\n__Далее:__\n" + fmt + f"\n**{len(upcoming)} песни в очереди**"
        embed = discord.Embed(title=f'Очередь для {ctx.guild.name}', description=fmt, color=discord.Color.green())
        embed.set_footer(text=f"{ctx.author.display_name}", icon_url=ctx.author.avatar_url)

        await ctx.send(embed=embed, delete_after=300)

    @commands.command(name='np', aliases=['song', 'current', 'currentsong', 'playing'],
                      description="Показывает текущую песню.")
    async def now_playing_(self, ctx):
        """Отображение информации о воспроизводимой в данный момент песне."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        player = self.get_player(ctx)
        if not player.current:
            embed = discord.Embed(title="", description="В настоящее время ничего не играет.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        seconds = vc.source.duration % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        if hour > 0:
            duration = "%dh %02dm %02ds" % (hour, minutes, seconds)
        else:
            duration = "%02dm %02ds" % (minutes, seconds)

        embed = discord.Embed(title="",
                              description=f"[{vc.source.title}]({vc.source.web_url}) [{vc.source.requester.mention}] | `{duration}`",
                              color=discord.Color.green())
        embed.set_author(icon_url=self.bot.user.avatar_url, name=f"Сейчас играет 🎶")
        await ctx.send(embed=embed, delete_after=300)

    @commands.command(name='volume', aliases=['vol', 'v'], description="Изменяет громкость")
    async def change_volume(self, ctx, *, vol: float = None):
        """Изменить громкость проигрывателя.
        Параметры
        ------------
        громкость: плавующая или целая [Required]
            Громкость, на которую нужно установить проигрыватель, в процентах. От 1 до 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        if not vol:
            embed = discord.Embed(title="", description=f"🔊 **{(vc.source.volume) * 100}%**",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        if not 0 < vol < 101:
            embed = discord.Embed(title="", description="Пожалуйста, вейдите значение от 1 до 100.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        embed = discord.Embed(title="", description=f'**`{ctx.author}`** Установлена громкость на **{vol}%**',
                              color=discord.Color.green())
        await ctx.send(embed=embed, delete_after=300)

    @commands.command(name='leave', aliases=["stop", "dc", "disconnect", "bye"],
                      description="Останавливает музыку и отключается от голосового канала.")
    async def leave_(self, ctx):
        """Остановить воспроизводимую в данный момент песню и уничтожить проигрыватель.
        !Предупреждение!
            Это приведет к уничтожению игрока, назначенного вашей гильдии, а также удалению всех песен и настроек в очереди.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="Я не подключён к голосовому каналу.",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed, delete_after=300)

        if (random.randint(0, 1) == 0):
            await ctx.message.add_reaction('👋')
        await ctx.send('**Успешно отключён.**', delete_after=300)

        await self.cleanup(ctx.guild)


def setup(bot):
    bot.add_cog(Music(bot))

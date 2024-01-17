import discord
from discord.ext import commands
from discord.utils import get
import music


client = commands.Bot( command_prefix = '$', intents = discord.Intents.all() )


@client.event
async def on_ready():
	print('Tofu Delivery connected')


# Music function
cogs = [music]

for i in range(len(cogs)):
	cogs[i].setup(client)


# Clean message
@client.command( pass_context = True )
async def clean(ctx, amount = 1):
	await ctx.channel.purge(limit = amount)


# Connect
token = open( '../info/token.txt', 'r' ).readline()
client.run( token )
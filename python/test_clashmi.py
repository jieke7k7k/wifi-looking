from clashmi import ClashMiClient
client = ClashMiClient()
print(client.settings)
print('connections', client.connection_count())

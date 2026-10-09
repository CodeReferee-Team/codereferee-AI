const http = require('http')

const port = process.env.PORT || 3000
const host = process.env.HOST || '0.0.0.0'

const server = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(req.url === '/health' ? '{"status":"healthy"}' : '{"status":"ok"}')
})

server.listen(port, host, () => console.log(`listening on ${host}:${port}`))

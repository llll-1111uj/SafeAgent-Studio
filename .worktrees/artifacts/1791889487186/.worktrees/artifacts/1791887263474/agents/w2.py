import urllib.request, socket, io, sys
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8",errors="replace")
socket.setdefaulttimeout(12)
hdr={"Referer":"http://www.weather.com.cn/","User-Agent":"Mozilla/5.0"}
u="http://d1.weather.com.cn/sk_2d/101060101.html"
d=urllib.request.urlopen(urllib.request.Request(u,headers=hdr)).read().decode("gbk","ignore")
print(d)
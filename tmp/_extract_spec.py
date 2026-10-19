import zipfile, re, io, sys
z = zipfile.ZipFile('素材人脸检索系统_完整实施规格v3_Windows版.docx')
xml = z.read('word/document.xml').decode('utf-8')
paras = re.split(r'</w:p>', xml)
out = []
for p in paras:
    txt = ''.join(re.findall(r'<w:t[^>]*>(.*?)</w:t>', p, re.S))
    txt = (txt.replace('&amp;','&').replace('&lt;','<').replace('&gt;','>').replace('&quot;','"'))
    out.append(txt)
open('tmp/spec.txt','w',encoding='utf-8').write('\n'.join(out))
print('paras', len(out))

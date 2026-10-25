"""Create a reviewed Chinese project report from public candidate data."""
import argparse,json,math
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,PageBreak,Table,TableStyle,Preformatted
from reportlab.graphics.shapes import Drawing,Rect,String,Line,Polygon
ROOT=Path(__file__).resolve().parents[1]
BLUE=colors.HexColor('#173852');TEAL=colors.HexColor('#117f8b');LIGHT=colors.HexColor('#eef4f7')

def make_chart(data,metric):
    d=Drawing(485,238);cols=['#286b8b','#d49634','#8466a2'];groups=['20% / low','20% / high','80% / low','80% / high']
    scenarios=['20short-low','20short-saturated','80short-low','80short-saturated']
    topologies=['mixed_fifo','pd_fifo','pd_fair']
    left=45;bottom=37;w=425;h=160
    d.add(Line(left,bottom,left+w,bottom,strokeColor=BLUE));d.add(Line(left,bottom,left,bottom+h,strokeColor=BLUE))
    for i,s in enumerate(scenarios):
        for j,t in enumerate(topologies):
            r=next(r for r in data if r['scenario']==s and r['topology']==t)
            v=r['completed_throughput_rps'] if metric=='throughput' else r['groups']['all']['ttft_s']['p95']*1000
            frac=v/2.2 if metric=='throughput' else (math.log10(v)-2)/3
            height=max(1,frac*h);x=left+20+i*102+j*21
            d.add(Rect(x,bottom,17,height,fillColor=colors.HexColor(cols[j]),strokeColor=None))
        d.add(String(left+15+i*102,19,groups[i],fontName='Helvetica',fontSize=9,fillColor=BLUE))
    ticks=[0,.5,1,1.5,2] if metric=='throughput' else [100,1000,10000,100000]
    for t in ticks:
        frac=t/2.2 if metric=='throughput' else (math.log10(t)-2)/3
        y=bottom+frac*h;d.add(Line(left-4,y,left+w,y,strokeColor=colors.HexColor('#dae3e8'),strokeWidth=.4))
        d.add(String(4,y-3,str(t),fontName='Helvetica',fontSize=8,fillColor=BLUE))
    for j,t in enumerate(topologies):
        x=55+j*140;d.add(Rect(x,218,10,10,fillColor=colors.HexColor(cols[j]),strokeColor=None))
        d.add(String(x+15,219,t,fontName='Helvetica',fontSize=9,fillColor=BLUE))
    d.add(String(50,202,'Completed requests/s' if metric=='throughput' else 'Client TTFT p95 (ms, log scale)',fontName='Helvetica',fontSize=9,fillColor=BLUE))
    return d

def architecture():
    d=Drawing(485,230)
    boxes=[(5,95,75,65,'Client','SSE/cancel'),(110,50,175,150,'PDServe','Admission / 3:1 / ACK'),(330,145,150,60,'Prefill','GPU0 / TP1'),(330,25,150,60,'Decode','GPU1 / TP1')]
    for x,y,w,h,a,b in boxes:
        d.add(Rect(x,y,w,h,rx=8,ry=8,fillColor=LIGHT,strokeColor=TEAL,strokeWidth=1.2))
        d.add(String(x+10,y+h-24,a,fontName='Helvetica-Bold',fontSize=13,fillColor=BLUE))
        d.add(String(x+10,y+h-43,b,fontName='Helvetica',fontSize=9,fillColor=BLUE))
    for a,b,c,e in [(80,127,110,127),(285,175,330,175),(285,55,330,55),(405,145,405,85)]:
        d.add(Line(a,b,c,e,strokeColor=TEAL,strokeWidth=1.5))
    d.add(String(338,112,'Mooncake KV',fontName='Helvetica',fontSize=10,fillColor=BLUE))
    return d

def main():
    p=argparse.ArgumentParser();p.add_argument('--font',required=True,type=Path)
    p.add_argument('--mono-font',type=Path);p.add_argument('--output',type=Path,default=ROOT/'docs/PDServe_项目报告.pdf')
    a=p.parse_args()
    if a.output.exists():raise ValueError('Refuse overwrite an existing PDF; use a new output path')
    pdfmetrics.registerFont(TTFont('CJK',str(a.font),subfontIndex=0))
    pdfmetrics.registerFontFamily('CJK',normal='CJK',bold='CJK',italic='CJK',boldItalic='CJK')
    if a.mono_font:pdfmetrics.registerFont(TTFont('Mono',str(a.mono_font)))
    else:pdfmetrics.registerFontFamily('Mono',normal='Courier')
    body=ParagraphStyle('body',fontName='CJK',fontSize=10,leading=16,spaceAfter=9,wordWrap='CJK',textColor=BLUE)
    title=ParagraphStyle('title',parent=body,fontSize=21,leading=29,spaceAfter=18)
    h2=ParagraphStyle('h2',parent=body,fontSize=13,leading=20,spaceBefore=10,spaceAfter=10)
    small=ParagraphStyle('small',parent=body,fontSize=8.5,leading=13)
    cell=ParagraphStyle('cell',parent=body,fontSize=8.5,leading=12,spaceAfter=0)
    code=ParagraphStyle('code',fontName='Mono' if a.mono_font else 'Courier',fontSize=8,leading=12,backColor=LIGHT,borderPadding=7,spaceAfter=12)
    story=[]
    def para(s,style=body):story.append(Paragraph(s,style))
    def heading(s):para(s,h2)
    def page(k,t):story.append(PageBreak());para(f'{k:02d}  {t}',title)
    def table(rows,widths):
        data=[[Paragraph(escape(str(v)),cell) for v in row] for row in rows]
        t=Table(data,colWidths=widths,hAlign='LEFT',repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),LIGHT),('LINEBELOW',(0,0),(-1,0),1,TEAL),('LINEBELOW',(0,1),(-1,-1),.3,colors.HexColor('#ccd8df')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
        story.append(t);story.append(Spacer(1,10))
    def command(s):story.append(Preformatted(s,code))
    data=json.loads((ROOT/'results/aggregates.json').read_text(encoding='utf-8'))['rows']
    para('PDServe',ParagraphStyle('cover',parent=title,fontSize=42,leading=54,spaceBefore=65))
    para('面向混合长短请求的<br/>Prefill / Decode 分离推理服务',title)
    para('开源候选与求职展示报告',h2)
    para('基于远程实际项目的源码、历史实验和证据审核',body)
    story.append(Spacer(1,32));story.append(architecture())
    para('公开候选：2026-10-25  |  固定推理模型：Qwen2.5-7B-Instruct',small)
    para('历史硬件：2 × A100 80GB；TP=1、PP=1；36次正式运行、1440个请求。',small)
    para('本轮只执行CPU、补丁重建、统计复核及文档检查；没有训练、下载模型或重新运行GPU。',small)
    page(1,'摘要与项目边界')
    para('长输入Prefill、逐token Decode和KV传输具有不同的资源特征。PDServe围绕容量准入、排队公平性与故障后的真实资源释放建立了一个可观测服务层，服务于单机双GPU研究场景。')
    table([['维度','本项目完成','不作的承诺'],['模型执行','复用SGLang；固定Qwen2.5-7B-Instruct BF16','没有训练、微调或模型质量提升'],['服务层','token/KV/slot预算、队列、SSE与取消','不是完整OpenAI API或生产平台'],['可靠性','真实清理ACK、隔离和显式epoch恢复','没有自动进程重启或分布式容灾'],['评测','真实推理历史、36次raw复算','不代表真实业务效果或容量极限']],[70,207,207])
    heading('可执行的交付')
    para('候选包含核心源码、固定上游补丁、无GPU测试、合成输入、脱敏统计、README、迭代历史与手动上传工具。完整原始响应和机器标识保留在私有证据中。公开摘要和原证据分别记录哈希，绝不混同。')
    para('实际远程提交为aef33d6；候选不是新的算法版本。SGLang执行模型，Mooncake执行通信，本项目新增服务编排和观测/生命周期逻辑。')
    page(2,'问题、架构与请求流程')
    story.append(architecture())
    para('客户端请求先经tokenizer精确编码；队列选择能同时容纳P/D预算的请求。Prefill计算完成后，Mooncake将KV交给Decode；后者逐步输出，网关把结果转成completion SSE。')
    para('响应结束不代表KV已释放。网关等待每个角色的allocator、metadata、compute和transport清理证明，并以epoch、内部请求ID和bootstrap room关联。资源无法确认时保留租约并隔离。')
    table([['组件','公开源码'],['预算与队列','src/pdserve/core.py'],['请求生命周期','src/pdserve/service.py'],['协议与API','protocol.py / api.py / backend.py'],['严格上下文边界','context_guard.py'],['引擎观测与清理','patches/ 与 engine_modules/'],['客户端统计','benchmarks/analyze.py / aggregate.py']],[170,314])
    page(3,'准入、调度与关键不变量')
    para('P预留输入token；D预留输入与最大输出之和，按KV page取整。每个节点预算不超过实际KV与slot容量的90%。单事件循环中的原子预留不含await，因此P/D不会只预留一半。')
    table([['参数','历史采用值','说明'],['上下文','16384','input+max_output必须严格小于上限'],['短请求阈值','2048 token','同类保持FIFO'],['公平轮转','S:S:S:L','可容纳的长请求等待≥5s后优先'],['有界队列','128；超时30s','专项验收使用queue4/active2'],['活动slot','参数32；有效29','历史引擎slot33，保留10%余量'],['混合基线','每副本32，总计64','相同GPU的架构对照，不是等总并发消融']],[100,140,244])
    heading('实现与性能证据分开')
    para('Stage2f专门构造排队，证明3:1、aging、队列拒绝和超时行为。36次性能运行均未发生网关派发重排，队列等待不足1ms；它们不能证明公平算法产生了性能改善。')
    page(4,'故障生命周期与恢复')
    para('取消receipt只证明意图已接收。P采用计算与传输排空；D在KV-ready之前保持资源，之后才允许cooperative abort。客户端断开也需要把取消传播到相关实例。')
    table([['事件','行为','释放条件'],['正常完成','保留响应终态和清理状态','两个角色分别收到完整cleanup ACK'],['取消/断连','显式终态，等待排空','compute=0及allocator/metadata/transport证明'],['节点退出/清理超时','整对隔离，拒绝新请求','不可自行清零未确认租约'],['成对恢复','旧进程退出，两个新epoch','旧租约标epoch_retired，不伪造ACK']],[104,183,197])
    para('历史故障验收覆盖排队、Prefill、传输、Decode取消、传输超时及P/D退出。公开候选保留相关逻辑和CPU证据检查；没有本轮GPU故障注入。')
    para('管理接口未鉴权，仅限本机实验。恢复接口信任人工old_pair_stopped确认，不具备独立进程退出证明。公开版没有通配杀进程或自动重启工具。')
    page(5,'输入与实验设计')
    table([['项目','设置'],['模型/精度','Qwen2.5-7B-Instruct；BF16；context16384'],['硬件','同一对A100-SXM4-80GB；TP=PP=1'],['输入/输出','512/8192输入；128/512输出，类内平衡'],['比例/到达','20%/80%短请求；0.25/2.0req/s开放环'],['规模','3配置×4场景×3重复×40请求=1440'],['控制','固定trace、seed2026、关闭前缀缓存'],['失败','正式运行全成功；1次失败尝试另存并报告']],[130,354])
    para('固定输入是合成token序列，不是业务数据集。没有模型准确率、训练loss或用户任务评分。模型文件本地SHA256已记录，上游revision未核实，重新下载的模型不能自动视为同字节。')
    para('历史目录的saturated是2req/s负载标签，并未做容量搜索或稳态判定。低负载吞吐由到达过程决定；有限窗口尾部排空影响完成吞吐。')
    page(6,'总体结果与负结果')
    story.append(make_chart(data,'throughput'))
    story.append(make_chart(data,'ttft'))
    para('每个柱对应3次重复共120请求；TTFT为客户端统计，延迟图使用对数轴。正式摘要均无失败/拒绝。图表描述该配置会话，不代表算法因果收益或最大吞吐。',small)
    para('双混合副本在本组试验中总体更好；P/D没有普遍提速。完整分类p50/p95/p99、ITL覆盖率和逐repeat统计见docs/RESULTS.md及results/。',small)
    page(7,'结果审查：哪些结论成立')
    para('80%短请求/2req/s时，P/D fair总体TTFT p95为752.86ms，FIFO为2297.31ms；完成吞吐分别1.9695和1.8362req/s。观测上约降低67.2%、提高7.3%，但没有网关重排，不能写成“公平调度算法带来67%优化”。')
    para('双副本同场景为2.0441req/s，fair仍低约3.6%。长请求占80%的高到达率场景，fair吞吐低于双副本约35.5%，TTFT明显退化。')
    table([['限制','证据与解释'],['重复波动','fair长请求高到达率TTFT p95约35.62s、10.71s、1.48s；含失败后重启条件'],['传输口径','Prefill完成到transfer_completed包含等待/worker/poll窗口，不是纯网络耗时'],['观测混杂','P/D每0.25s获取阶段状态；mixed每1s轮询metrics'],['ITL口径','只统计+1帧；配置覆盖率不同，不是全token无偏分布'],['尾部样本','每类别可能只有24请求，p99仅为样本描述']],[100,384])
    para('下一步需要真正触发队列的匹配并发消融、随机交错配置、统一观测和闭环在途Prefill限制。本轮不重跑模型来补造证据。')
    page(8,'实际迭代与采用版本')
    table([['组件阶段','相比前版变化','验证/最终采用'],['模型/适配器','无；同一7B模型，无训练','本地hash；revision缺口保留'],['Stage1','固定环境与stock P/D链路','单卡及真实双卡；底座'],['Stage2a/b/b2/c1','阶段事件、真实ACK、取消、超时guard','CPU+历史双卡；最终合并10文件'],['Stage2d/e/f','网关预算、严格context、节点退出、调度专项','CPU+专项真卡；核心字节保留'],['Stage3a/b/c/d','客户端测量、双副本/PD基线、硬件绑定campaign','最终36次；未拼接旧卡数据'],['Stage3d fix1-6','超时、失败sidecar、ITL口径、mixed轮询策略','测量工具修订，不是算法升级'],['Stage4/公开候选','私有交付转公开范围、便携入口、PDF与CI','本轮CPU重建；新入口未GPU验收']],[104,200,180])
    para('详细历史按模型、输入、工具执行、算法、评测与发布分别记录在docs/ITERATION_HISTORY.md。旧ZIP修订不当作性能提升版本；原始历史验收清单没有被刷新。')
    page(9,'最小示例与故障演示')
    heading('无GPU：Windows或Linux，候选目录')
    command('python tools/check.py\npython examples/mock_scheduler.py\npython tools/summarize_results.py')
    para('mock示例只展示合成队列；不声称KV传输或真实模型服务。本轮未启动任何GPU进程。')
    heading('真实Linux服务：用户手动执行')
    command('python scripts/serve_backend.py prefill --model-path "$MODEL"\npython scripts/serve_backend.py decode --model-path "$MODEL"\npython -m pdserve.launch fair --model-path "$MODEL"\npython examples/sse_client.py --request-id demo-001')
    para('每条服务命令在独立终端运行，先完成依赖、模型与补丁设置。使用cancel接口观察receipt、终态和ACK分离；节点退出只关闭自己启动的后端终端，保留网关隔离状态。')
    para('恢复必须确认旧P/D都已停止并启动新epoch。不要把人工old_pair_stopped=true当成自动进程证明，不要自动重试已开始输出的请求。详见docs/DEMO.md。')
    page(10,'依赖、重建与新实验')
    para('历史环境固定SGLang0.4.6.post5、torch2.6.0+cu124、FlashInfer0.2.5+cu124torch2.6、sgl-kernel0.1.4、Mooncake0.3.2、transformers4.51.1和compressed-tensors0.9.3。完整freeze在requirements/。')
    command('python tools/rebuild_engine.py \\\n  --upstream work/upstream-sglang/python \\\n  --output work/patched-site')
    para('重建只写新目录，before不匹配就停止；10个after hash全部检查。本轮用证据中的stock字节重建成功，没有重新下载上游或安装GPU包。')
    command('export PYTHONPATH="$PWD/src:$PWD/work/patched-site:$PWD/benchmarks"\npython scripts/preflight.py pd_fair --model-path "$MODEL"\npython benchmarks/run_pilot.py pd_fair --campaign my-new-run\npython benchmarks/run_matrix.py pd_fair --campaign my-new-run')
    para('新campaign绑定当前硬件、模型/依赖和候选清单；变化时拒绝续跑。模型revision缺口、旧wheel可用性及新目录GPU部署均明确未验证。完整分平台命令以README和REPRODUCE为准。')
    page(11,'公开范围、来源与许可')
    para('保留：服务实现、必要测试、固定上游补丁、合成trace、脱敏组统计和说明。排除：权重、环境、旧.git、完整第三方树、原始响应、主机名、GPU UUID、boot ID、内部地址和历史归档。')
    para('464个私有采集文件通过哈希；12个历史evidence归档与远程哈希一致；37份raw逐文件匹配；36份summary从frames复算一致。公开产物独立记录自己的hash与提取规则，不伪装成原始证据。')
    para('许可：候选采用Apache-2.0，保留已安装SGLang许可证及上游归属。只有上游修改diff与项目新增模块入库；Mooncake、FlashInfer、PyTorch和Qwen权重通过外链使用。个人作者身份和GitHub地址没有编造。')
    para('模型效果、系统效果、模拟测试和真实推理历史分别标注。项目使用AI辅助开发与文档整理，远程实验由持有人执行；求职展示应说明个人实际理解和操作。')
    heading('参考资源')
    for s in ['SGLang fixed tag: github.com/sgl-project/sglang/tree/v0.4.6.post5','Mooncake fixed tag: github.com/kvcache-ai/Mooncake/tree/v0.3.2','Qwen model: huggingface.co/Qwen/Qwen2.5-7B-Instruct']:
        para(escape(s),small)
    page(12,'验收记录与交付使用')
    table([['检查','本轮结果/范围'],['审查来源','远程盘点+审查包；旧附件只在hash一致后复用'],['原始统计','36份summary复算一致；37份raw hash匹配'],['核心单元测试','见docs/CHECKS；标准库CPU运行，无GPU/网络'],['补丁重建','10个最终目标与远程接受的字节hash一致'],['源码保持','继承核心原字节保存；便携入口单列适配'],['文档/PDF','相对链接、公开范围、图表口径和逐页渲染检查'],['未执行','GPU启动、依赖安装、模型下载、训练、业务/生产验证'],['GitHub','用户手动上传；未自动commit/push，无凭据访问']],[112,372])
    para('最终交付包含独立GitHub候选ZIP、本中文PDF、详细README和迭代历史、来源映射、公开范围/缺口、检查记录，以及Windows手动上传脚本。候选没有Git历史，避免携带私有信息；已有仓库采用干净工作区上的增量更新，不重新初始化或强推。')
    para('本报告的主要价值是可审阅的服务实现和诚实的证据边界。它不把PD等同于提速，也不把模拟与静态检查当作真实模型验收。')
    def footer(canvas,doc):
        canvas.saveState();canvas.setStrokeColor(TEAL);canvas.line(48,43,547,43)
        canvas.setFont('CJK',8);canvas.setFillColor(BLUE)
        canvas.drawString(48,29,'PDServe | 公开候选 2026-10-25 | 证据边界见正文')
        canvas.drawRightString(547,29,str(doc.page));canvas.restoreState()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    doc=SimpleDocTemplate(str(a.output),pagesize=A4,rightMargin=50,leftMargin=50,topMargin=48,bottomMargin=58,
        title='PDServe 项目报告',author='PDServe project',subject='Remote-evidence-based public candidate and system results')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    print(a.output.resolve())
if __name__=='__main__':main()

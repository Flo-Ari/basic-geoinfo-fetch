"""Monotonic, approximate whole-dataset progress from measured stage events."""

PLANS = {
    'dem': {'准备数据':(0,5), '下载':(5,45), '拼接栅格':(45,65), '准备裁剪':(65,70), '裁剪':(70,90), '写入成果':(90,99)},
    'population': {'准备数据':(0,5), '下载':(5,70), '准备裁剪':(70,72), '裁剪':(72,95), '写入成果':(95,99)},
    'roads': {'准备数据':(0,5), '下载':(5,35), '转换 PBF':(35,55), '建立空间索引':(55,75), '裁剪':(75,95), '写入成果':(95,99)},
    'hydro': {'准备数据':(0,5), '下载':(5,35), '转换 PBF':(35,55), '建立空间索引':(55,75), '裁剪':(75,95), '写入成果':(95,99)},
    'landcover': {'准备数据':(0,10), '拼接栅格':(10,60), '准备裁剪':(60,65), '裁剪':(65,90), '写入成果':(90,99)},
    'euluc': {'准备数据':(0,10), '裁剪':(10,90), '写入成果':(90,99)},
    'events': {'准备数据':(0,5), '下载':(5,65), '提取 PDF':(65,95), '提取事件':(0,99)},
}

class DatasetProgress:
    def __init__(self, dataset):
        self.dataset = dataset
        self.percent = 0.
        self.events_done = 0
        self.events_total = None

    def update(self, event):
        stage = event.get('stage', '')
        lower, upper = PLANS.get(self.dataset, {}).get(stage, (self.percent, self.percent))
        measured = event.get('percent')
        candidate = lower if measured is None else lower + (upper-lower) * measured / 100.
        if self.dataset == 'events':
            if stage == '提取事件' and event.get('total'):
                self.events_done = event.get('completed', 0)
                self.events_total = event['total']
                candidate = 99. * self.events_done / self.events_total
            elif self.events_total:
                candidate = 99. * (self.events_done + candidate / 100.) / self.events_total
        # Retries, repeated product stages and late callbacks must not rewind.
        self.percent = round(min(99., max(self.percent, candidate)), 1)
        return dict(event, dataset=self.dataset, overall_percent=self.percent, overall_estimated=True)

    def finish(self):
        self.percent = 100.
        return dict(stage='已完成', percent=100., dataset=self.dataset, overall_percent=100., overall_estimated=False)

"""Window-based raster operations with measured progress, matching mask defaults."""
import math
import numpy as np
from rasterio.features import geometry_mask, geometry_window
from rasterio.windows import Window, transform
from .progress import report

def merge(*args, **kwargs):
    from rasterio.merge import merge as raster_merge
    report('拼接栅格', force=True)
    result = raster_merge(*args, **kwargs)
    report('拼接栅格', 1, 1, force=True)
    return result

def mask(dataset, shapes, crop=True, nodata=None, filled=True, block_size=512):
    shapes = list(shapes)
    nodata = dataset.nodata if nodata is None else nodata
    nodata = 0 if nodata is None else nodata
    window = geometry_window(dataset, shapes) if crop else Window(0, 0, dataset.width, dataset.height)
    width, height = int(window.width), int(window.height)
    output_transform = transform(window, dataset.transform)
    report('准备裁剪', force=True)
    # Rasterize once on the original output grid. Re-rasterizing per tile can
    # change edge pixels through floating-point transform rounding.
    outside = geometry_mask(shapes, out_shape=(height, width), transform=output_transform)
    output = np.empty((dataset.count, height, width), dtype=dataset.dtypes[0])
    output_mask = np.empty(output.shape, dtype=bool) if not filled else None
    total = math.ceil(height / block_size) * math.ceil(width / block_size)
    report('裁剪', 0, total, '窗口', force=True)
    done = 0
    for row in range(0, height, block_size):
        for col in range(0, width, block_size):
            h, w = min(block_size, height-row), min(block_size, width-col)
            current = Window(window.col_off+col, window.row_off+row, w, h)
            data = dataset.read(window=current, masked=True)
            data.mask = np.ma.getmaskarray(data) | outside[row:row+h, col:col+w]
            output[:, row:row+h, col:col+w] = data.filled(nodata) if filled else data.data
            if not filled:
                output_mask[:, row:row+h, col:col+w] = data.mask
            done += 1
            report('裁剪', done, total, '窗口')
    return (output if filled else np.ma.array(output, mask=output_mask)), output_transform

def write(dataset, data, block_size=512):
    height, width = data.shape[-2:]
    total = math.ceil(height/block_size) * math.ceil(width/block_size)
    done = 0
    report('写入成果', 0, total, '窗口', force=True)
    for row in range(0, height, block_size):
        for col in range(0, width, block_size):
            h, w = min(block_size, height-row), min(block_size, width-col)
            dataset.write(data[:, row:row+h, col:col+w], window=Window(col,row,w,h))
            done += 1
            report('写入成果', done, total, '窗口')

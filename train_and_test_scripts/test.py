import torch
import numpy as np
from skimage.feature import canny
from skimage.color import rgb2gray
from inpainting_methods.CTSDG_inpainting.models.generator.generator import Generator
from PIL import Image
import torchvision.transforms.functional as F

def to_tensor(img):
    img = Image.fromarray(img)
    img_t = F.to_tensor(img).float()
    return img_t

generator = Generator(image_in_channels=3, edge_in_channels=2, out_channels=3).cuda()
input = torch.ones((1,3,256,256)).cuda()
grid = input[0]

img_GT = grid.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).contiguous().to('cpu', torch.uint8).numpy()

img_gray = rgb2gray(img_GT)
img_gray = img_gray.astype(np.float)
sigma = 2 #random.randint(1, 4)
canny_img = canny(img_gray, sigma=sigma, mask=None)
canny_img = canny_img.astype(np.float)

img_GT = torch.from_numpy(np.ascontiguousarray(np.transpose(img_GT, (2, 0, 1)))).float().unsqueeze(0).cuda()
img_gray = to_tensor(canny_img).unsqueeze(0).cuda()
canny_img = to_tensor(canny_img).unsqueeze(0).cuda()

output, _, _ = generator(img_GT, torch.cat((canny_img, img_gray), dim=1), canny_img)
print(output.shape)

from MVSS.models.resfcn import ResFCN
discriminator_mask = ResFCN().cuda()
checkpoint = torch.load('./resfcn_coco_1013.pth', map_location='cpu')
discriminator_mask.load_state_dict(checkpoint, strict=True)
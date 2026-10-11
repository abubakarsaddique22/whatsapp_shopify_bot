"""Picture choice without a database (Phase 3, step 6)."""

from app.catalog.images import ImageInfo, VariantImage, pick_images

FEATURED = ImageInfo(url="https://cdn.example/a.jpg?v=1", alt="front")
GALLERY = [
    FEATURED,
    ImageInfo(url="https://cdn.example/blue.jpg?v=9"),
    ImageInfo(url="https://cdn.example/b.jpg"),
    ImageInfo(url="https://cdn.example/d.jpg"),
]
BLACK = VariantImage("v1", {"Colour": "Black"}, "https://cdn.example/black.jpg")
BLUE = VariantImage("v2", {"Colour": "Blue"}, "https://cdn.example/blue.jpg?v=1")
BLUE_NO_PHOTO = VariantImage("v3", {"Colour": "Red"}, None)


def urls(images):
    return [i.url for i in images]


def test_default_is_one_featured_picture():
    images, colour, found = pick_images(GALLERY, [BLACK, BLUE])
    assert urls(images) == ["https://cdn.example/a.jpg?v=1"]
    assert images[0].source == "featured"
    assert colour is None
    assert found is None


def test_named_colour_gets_that_variants_own_picture():
    images, colour, found = pick_images(GALLERY, [BLACK, BLUE], colour="neela")  # Roman Urdu
    assert urls(images) == ["https://cdn.example/blue.jpg?v=1"]
    assert images[0].source == "variant"
    assert colour == "blue"
    assert found is True


def test_colour_without_its_own_picture_falls_back_to_the_featured_one():
    images, colour, found = pick_images(GALLERY, [BLACK, BLUE_NO_PHOTO], colour="red")
    assert urls(images) == ["https://cdn.example/a.jpg?v=1"]
    assert images[0].source == "featured"
    assert colour == "red"
    assert found is False


def test_colour_the_product_does_not_have_also_falls_back_and_says_so():
    _, _, found = pick_images(GALLERY, [BLACK, BLUE], colour="green")
    assert found is False


def test_a_given_variant_wins_over_the_colour():
    images, _, found = pick_images(GALLERY, [BLACK, BLUE], variant_id="v1", colour="blue")
    assert urls(images) == ["https://cdn.example/black.jpg"]
    assert found is None


def test_more_pictures_is_three_at_most_and_without_duplicates():
    images, _, _ = pick_images(GALLERY, [BLACK, BLUE], colour="blue", max_images=10)
    assert len(images) == 3
    # blue.jpg is also in the gallery (with another ?v=): it must appear once
    assert sum("blue.jpg" in url for url in urls(images)) == 1
    assert images[0].source == "variant"
    assert urls(images)[1:] == ["https://cdn.example/a.jpg?v=1", "https://cdn.example/b.jpg"]


def test_max_images_is_at_least_one():
    images, _, _ = pick_images(GALLERY, [], max_images=0)
    assert len(images) == 1


def test_no_pictures_at_all_gives_an_empty_list():
    no_photo = VariantImage("v9", {}, None)
    images, _, found = pick_images([], [no_photo])
    assert images == []
    assert found is None


def test_variant_picture_is_used_even_when_the_gallery_is_empty():
    images, _, _ = pick_images([], [BLACK], colour="black")
    assert urls(images) == ["https://cdn.example/black.jpg"]
